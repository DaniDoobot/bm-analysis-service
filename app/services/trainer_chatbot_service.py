"""
Trainer Chatbot Service — Unified Scoped RAG for voice training knowledge.
Supports both text and audio input through a single reasoning and retrieval pipeline.
"""
import io
import json
import logging
from typing import Any, List, Optional, Tuple

from fastapi import HTTPException, UploadFile, status
from starlette.datastructures import UploadFile as StarletteUploadFile
from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.roles import InternalRole, normalize_role
from app.core.tenant_context import TenantContext
from app.models.personalized_training import TrainingKnowledgeDocument
from app.models.users import User
from app.routers.personalized_training import enforce_agent_or_admin_ownership
from app.services import openai_service

logger = logging.getLogger(__name__)

# Constants
MAX_AUDIO_SIZE_BYTES = 15 * 1024 * 1024  # 15 MB
ALLOWED_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".webm", ".ogg", ".aac", ".flac"}
MAX_HISTORY_MESSAGES = 8
MAX_KNOWLEDGE_DOCUMENTS = 12


class TrainerChatbotService:
    """Service orchestrating input parsing, scoping, knowledge retrieval, and grounded RAG responses."""

    @staticmethod
    async def validate_and_extract_input(
        message: Optional[str] = None,
        audio_file: Optional[Any] = None,
    ) -> Tuple[str, str]:
        """
        Validates input presence and exclusivity (either text OR audio, exactly one).
        If audio is provided, transcribes it into normalized query text.

        Returns:
            Tuple[str, str]: (query_text, input_type) where input_type is 'text' or 'audio'.
        """
        has_message = bool(message and str(message).strip())
        has_audio = (
            audio_file is not None
            and (isinstance(audio_file, (UploadFile, StarletteUploadFile)) or hasattr(audio_file, "file"))
        )

        if not has_message and not has_audio:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Debe proporcionar un mensaje de texto o un archivo de audio.",
            )

        if has_message and has_audio:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Debe enviar únicamente texto o audio, no ambos simultáneamente.",
            )

        if has_message:
            query_text = str(message).strip()
            return query_text, "text"

        # Audio branch
        filename = getattr(audio_file, "filename", None) or "audio.mp3"
        ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
        if ext not in ALLOWED_AUDIO_EXTENSIONS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Formato de audio no permitido ('{ext}'). Formatos soportados: {', '.join(sorted(ALLOWED_AUDIO_EXTENSIONS))}.",
            )

        try:
            audio_bytes = await audio_file.read()
        except Exception as read_err:
            logger.exception("Error leyendo bytes del archivo de audio: %s", read_err)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No se pudo leer el archivo de audio subido.",
            )
        finally:
            try:
                await audio_file.close()
            except Exception:
                pass

        if not audio_bytes or len(audio_bytes) == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="El archivo de audio está vacío.",
            )

        if len(audio_bytes) > MAX_AUDIO_SIZE_BYTES:
            max_mb = MAX_AUDIO_SIZE_BYTES // (1024 * 1024)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"El archivo de audio supera el tamaño máximo permitido ({max_mb} MB).",
            )

        try:
            transcription_res = await openai_service.transcribe_audio(audio_bytes, filename=filename)
        except Exception as e:
            logger.exception("Error durante la transcripción de audio en Trainer Chatbot: %s", e)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Fallo en el servicio de transcripción de audio: {str(e)}",
            )

        raw_text = (
            transcription_res.get("text", "")
            if isinstance(transcription_res, dict)
            else str(transcription_res or "")
        )
        query_text = raw_text.strip()
        if not query_text:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No se pudo transcribir ningún texto comprensible a partir del audio proporcionado.",
            )

        return query_text, "audio"

    @staticmethod
    def resolve_target_agent(
        current_user: User,
        requested_agent_id: Optional[str],
        context: TenantContext,
    ) -> str:
        """
        Determines the target hubspot_owner_id with strict tenant security:
        - AGENT: ALWAYS forced to current_user.hubspot_owner_id, ignoring any frontend requested_agent_id.
        - SUPERVISOR / COORDINATOR / ADMIN: validated against context scope.
        """
        norm_role = normalize_role(current_user.role)

        if norm_role == InternalRole.AGENT:
            if not current_user.hubspot_owner_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Tu cuenta de usuario no está asociada a ningún HubSpot Owner ID.",
                )
            return current_user.hubspot_owner_id

        # Supervisors, coordinators, and admins
        if requested_agent_id and requested_agent_id.strip():
            target_agent = requested_agent_id.strip()
            # Reuse existing ownership validator
            enforce_agent_or_admin_ownership(current_user, target_agent, context)

            if context.allowed_agent_ids is not None and target_agent not in context.allowed_agent_ids:
                logger.warning(
                    "Security violation: user %s (%s) tried to query agent %s outside allowed_agent_ids %s",
                    current_user.user_id,
                    current_user.email,
                    target_agent,
                    context.allowed_agent_ids,
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No tienes permisos para consultar la formación de este agente.",
                )
            return target_agent

        # Fallback if no agent_id requested: check if user has own hubspot_owner_id
        if current_user.hubspot_owner_id:
            return current_user.hubspot_owner_id

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debes especificar el 'agent_id' sobre el que deseas realizar la consulta.",
        )

    @staticmethod
    async def fetch_knowledge_documents(
        db: AsyncSession,
        context: TenantContext,
        target_agent_id: str,
        cycle_id: Optional[int] = None,
    ) -> List[TrainingKnowledgeDocument]:
        """
        Retrieves deterministic TrainingKnowledgeDocument records scoped by:
        - company_id (mandatory tenant isolation)
        - hubspot_owner_id == target_agent_id
        - cycle_id (optional filter)
        Deterministic SQL query on structured documents.
        """
        stmt = select(TrainingKnowledgeDocument).where(
            TrainingKnowledgeDocument.hubspot_owner_id == target_agent_id
        )

        # Multi-tenant scoping
        if context.company_id is not None:
            stmt = stmt.where(TrainingKnowledgeDocument.company_id == context.company_id)
        elif not context.is_super_admin and context.allowed_company_ids:
            stmt = stmt.where(TrainingKnowledgeDocument.company_id.in_(context.allowed_company_ids))

        if cycle_id is not None:
            stmt = stmt.where(TrainingKnowledgeDocument.cycle_id == cycle_id)
            stmt = stmt.order_by(
                TrainingKnowledgeDocument.document_type.desc(),  # 'simulation' before 'cycle'
                TrainingKnowledgeDocument.created_at.desc(),
                TrainingKnowledgeDocument.id.desc(),
            ).limit(20)
        else:
            stmt = stmt.order_by(
                TrainingKnowledgeDocument.created_at.desc(),
                TrainingKnowledgeDocument.id.desc(),
            ).limit(MAX_KNOWLEDGE_DOCUMENTS)

        res = await db.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    def sanitize_history(conversation_history_raw: Any) -> List[dict[str, str]]:
        """
        Parses and validates conversation history, limiting it to MAX_HISTORY_MESSAGES
        to prevent unbounded prompt growth.
        """
        if not conversation_history_raw:
            return []

        parsed = conversation_history_raw
        if isinstance(conversation_history_raw, str):
            try:
                parsed = json.loads(conversation_history_raw)
            except Exception:
                logger.debug("Failed parsing conversation_history as JSON; treating as empty.")
                return []

        if not isinstance(parsed, list):
            return []

        cleaned: List[dict[str, str]] = []
        for msg in parsed:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            content = msg.get("content")
            if role in ["user", "assistant"] and isinstance(content, str) and content.strip():
                # Bound maximum individual message length to 2000 chars defensively
                cleaned.append({
                    "role": role,
                    "content": content.strip()[:2000],
                })

        return cleaned[-MAX_HISTORY_MESSAGES:]

    @staticmethod
    def build_grounding_prompt(
        documents: List[TrainingKnowledgeDocument],
        target_agent_id: str,
    ) -> str:
        """
        Builds the strict Grounding System Prompt incorporating available training knowledge documents.
        Enforces factual fidelity, quotation of evidence, and transparency if info is missing.
        """
        lines = [
            "Eres el Asistente y Tutor de Formación de Trainer (Speech BM).",
            "Tu misión es responder a las preguntas y dudas sobre el entrenamiento, simulaciones y desempeño "
            f"del agente con ID '{target_agent_id}'.",
            "",
            "REGLAS OBLIGATORIAS DE COMPORTAMIENTO Y GROUNDING:",
            "1. Responde ÚNICAMENTE basándote en los Documentos de Conocimiento oficiales proporcionados a continuación.",
            "2. NUNCA inventes notas, puntuaciones, evidencias, transcripciones ni hechos que no consten en los documentos.",
            "3. Si la respuesta a la pregunta no está registrada en los documentos o falta información, indícalo claramente con total honestidad.",
            "4. Cuando analices una simulación o evaluación, identifica con precisión el Ciclo formativo, el número de Simulación, el título y el Criterio evaluado.",
            "5. Cita textualmente las evidencias de la llamada ('evidencia textual') cuando justifiques un resultado o una recomendación.",
            "6. Diferencia con claridad los hechos constatados (lo que ocurrió en la llamada) de las sugerencias o consejos pedagógicos de mejora.",
            "7. Adopta siempre una actitud de tutor experto, analítica, constructiva, orientada a la mejora continua y empática.",
            "",
        ]

        if not documents:
            lines.append("--- BASE DE CONOCIMIENTO ---")
            lines.append(
                "ADVERTENCIA CRÍTICA: No existen documentos de conocimiento ni evaluaciones registradas para este agente "
                "en el contexto o ciclo especificado. Informa al usuario de que no hay datos disponibles sin inventar información."
            )
            lines.append("----------------------------")
            return "\n".join(lines)

        lines.append(f"--- BASE DE CONOCIMIENTO DISPONIBLE ({len(documents)} DOCUMENTOS) ---")
        for doc in documents:
            lines.append(f"### [DOCUMENTO ID #{doc.id}] Título: {doc.title}")
            lines.append(f"- Tipo: {doc.document_type} | Ciclo ID: {doc.cycle_id} | Simulación ID: {doc.simulation_id or 'N/A'}")
            lines.append(f"- Metadatos: {json.dumps(doc.metadata_json, ensure_ascii=False)}")
            lines.append("Contenido del Documento:")
            lines.append(doc.content)
            lines.append("---")

        lines.append("FIN DE LA BASE DE CONOCIMIENTO.")
        return "\n".join(lines)

    @classmethod
    async def process_chat(
        cls,
        db: AsyncSession,
        current_user: User,
        context: TenantContext,
        message: Optional[str] = None,
        audio_file: Optional[UploadFile] = None,
        agent_id: Optional[str] = None,
        cycle_id: Optional[int] = None,
        conversation_history: Any = None,
    ) -> dict[str, Any]:
        """
        Executes the full unified Chatbot pipeline:
        1. Input validation & audio transcription (converges to query_text).
        2. Strict tenant security & target agent resolution.
        3. Scoped knowledge document retrieval.
        4. Grounded system prompt & conversation history assembly.
        5. AI Provider text completion.
        6. Structured response payload.
        """
        # 1. Validate & extract query text (audio or text)
        query_text, input_type = await cls.validate_and_extract_input(
            message=message,
            audio_file=audio_file,
        )

        # 2. Resolve target agent with strict tenant isolation
        target_agent_id = cls.resolve_target_agent(
            current_user=current_user,
            requested_agent_id=agent_id,
            context=context,
        )

        # 3. Fetch knowledge documents
        documents = await cls.fetch_knowledge_documents(
            db=db,
            context=context,
            target_agent_id=target_agent_id,
            cycle_id=cycle_id,
        )

        # 4. Build prompt and prepare message list
        system_instruction = cls.build_grounding_prompt(
            documents=documents,
            target_agent_id=target_agent_id,
        )

        history = cls.sanitize_history(conversation_history)

        messages = [{"role": "system", "content": system_instruction}]
        messages.extend(history)
        messages.append({"role": "user", "content": query_text})

        # 5. Execute LLM completion via existing abstraction
        try:
            response_text = await openai_service.complete_text(
                messages=messages,
                temperature=0.2,
                response_format=None,
            )
        except Exception as llm_err:
            logger.exception("Error invocando complete_text en Trainer Chatbot: %s", llm_err)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Error en el modelo de lenguaje del chatbot: {str(llm_err)}",
            )

        # 6. Format sources
        sources = [
            {
                "document_id": doc.id,
                "title": doc.title,
                "document_type": doc.document_type,
                "cycle_id": doc.cycle_id,
            }
            for doc in documents
        ]

        return {
            "response": response_text or "",
            "user_query": query_text,
            "input_type": input_type,
            "sources": sources,
        }
