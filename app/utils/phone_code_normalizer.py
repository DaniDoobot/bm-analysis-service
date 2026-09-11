"""
phone_code_normalizer.py
========================
Phonetic and text normalization for agent training codes in voice/telephony channels.

Supports:
1. Alphanumeric codes:
   - Direct: "FR45", "cm 77", "VA-12", "L.D.23"
   - Spoken Spanish phonetic:
     "efe erre cuarenta y cinco" -> "FR45"
     "a be cuarenta y siete"     -> "AB47"
     "L D veintitrés"            -> "LD23"
     "ele de veintitres"         -> "LD23"
     "ce eme setenta y siete"    -> "CM77"

2. Numeric codes (4 digits):
   - Direct digits: "7777", "5831", "4545"
   - Spoken digits / phrases:
     "cinco ocho tres uno"               -> "5831"
     "siete siete siete siete"           -> "7777"
     "cuarenta y cinco cuarenta y cinco" -> "4545"
     "siete mil setecientos setenta y siete" -> "7777"
"""
import re
from typing import List, Optional

SPANISH_LETTER_WORDS = {
    "a": "A",
    "be": "B", "b": "B",
    "ce": "C", "c": "C",
    "de": "D", "d": "D",
    "e": "E",
    "efe": "F", "f": "F",
    "ge": "G", "g": "G",
    "hache": "H", "h": "H",
    "i": "I",
    "jota": "J", "j": "J",
    "ka": "K", "k": "K",
    "ele": "L", "l": "L",
    "eme": "M", "m": "M",
    "ene": "N", "n": "N",
    "o": "O",
    "pe": "P", "p": "P",
    "cu": "Q", "q": "Q",
    "erre": "R", "ere": "R", "r": "R",
    "ese": "S", "s": "S",
    "te": "T", "t": "T",
    "u": "U",
    "uve": "V", "ve": "V", "v": "V",
    "equis": "X", "x": "X",
    "ye": "Y", "y": "Y",
    "zeta": "Z", "ceta": "Z", "z": "Z",
}

SPANISH_UNITS_TEENS = {
    "cero": 0, "uno": 1, "un": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
    "seis": 6, "siete": 7, "ocho": 8, "nueve": 9,
    "diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14, "quince": 15,
    "dieciseis": 16, "diecisiete": 17, "dieciocho": 18, "diecinueve": 19,
    "veinte": 20, "veintiuno": 21, "veintiun": 21, "veintidos": 22, "veintitres": 23,
    "veinticuatro": 24, "veinticinco": 25, "veintiseis": 26, "veintisiete": 27,
    "veintiocho": 28, "veintinueve": 29
}

SPANISH_TENS = {
    "diez": 10, "veinte": 20, "treinta": 30, "cuarenta": 40, "cincuenta": 50,
    "sesenta": 60, "setenta": 70, "ochenta": 80, "noventa": 90
}

SPANISH_HUNDREDS = {
    "cien": 100, "ciento": 100, "doscientos": 200, "trescientos": 300,
    "cuatrocientos": 400, "quinientos": 500, "seiscientos": 600,
    "setecientos": 700, "ochocientos": 800, "novecientos": 900
}


def clean_spanish_text(text: str) -> str:
    cleaned = text.lower().strip()
    replacements = {
        "á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u", "ü": "u",
        ",": " ", ".": " ", "-": " ", "_": " ", "/": " ", "#": " "
    }
    for k, v in replacements.items():
        cleaned = cleaned.replace(k, v)
    return re.sub(r"\s+", " ", cleaned).strip()


def parse_number_tokens_strict(tokens: List[str]) -> Optional[List[int]]:
    """
    Parse a list of word tokens into numbers strictly.
    Returns None if ANY token in the list is not a recognized number component.
    """
    result = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]

        if tok.isdigit():
            result.append(int(tok))
            i += 1
            continue

        # Tens + "y" + units (e.g. "cuarenta y cinco" -> 45)
        if (
            tok in SPANISH_TENS
            and i + 2 < len(tokens)
            and tokens[i + 1] == "y"
            and tokens[i + 2] in SPANISH_UNITS_TEENS
        ):
            val = SPANISH_TENS[tok] + SPANISH_UNITS_TEENS[tokens[i + 2]]
            result.append(val)
            i += 3
            continue

        if tok in SPANISH_UNITS_TEENS:
            result.append(SPANISH_UNITS_TEENS[tok])
            i += 1
            continue

        if tok in SPANISH_TENS:
            result.append(SPANISH_TENS[tok])
            i += 1
            continue

        # Any token not recognized as a number causes strict parse failure
        return None

    return result


def normalize_spoken_code(raw_input: str) -> Optional[str]:
    """
    Normalize raw user input (spoken transcription or formatted code)
    into a canonical training_code (AA99) or training_numeric_code (4 digits).
    Strictly avoids false positives on arbitrary phrases.
    """
    if not raw_input or len(raw_input) > 60:
        return None

    cleaned = clean_spanish_text(raw_input)
    if not cleaned:
        return None

    no_spaces = cleaned.replace(" ", "").upper()

    # 1. Direct standard formats: AA99 or 4 digits
    if re.match(r"^[A-Z]{2}\d{2}$", no_spaces):
        return no_spaces
    if re.match(r"^\d{4}$", no_spaces):
        return no_spaces

    tokens = cleaned.split()
    if not tokens or len(tokens) > 7:
        return None

    # 2. Check if starts with 1 or 2 letter words (e.g. "efe erre", "a be", "L D")
    letter_parts = []
    idx = 0
    while idx < len(tokens) and len(letter_parts) < 2:
        tok = tokens[idx]
        if tok in SPANISH_LETTER_WORDS:
            if tok == "y" and idx > 0 and tokens[idx - 1] in SPANISH_TENS:
                break
            letter_parts.append(SPANISH_LETTER_WORDS[tok])
            idx += 1
        elif len(tok) == 1 and tok.isalpha():
            letter_parts.append(tok.upper())
            idx += 1
        elif len(tok) == 2 and tok.isalpha() and tok not in SPANISH_UNITS_TEENS and tok not in SPANISH_TENS:
            letter_parts.extend([tok[0].upper(), tok[1].upper()])
            idx += 1
        else:
            break

    if len(letter_parts) == 2:
        remaining_tokens = tokens[idx:]
        if remaining_tokens:
            num_chunks = parse_number_tokens_strict(remaining_tokens)
            if num_chunks:
                if len(num_chunks) == 1 and 0 <= num_chunks[0] <= 99:
                    return f"{letter_parts[0]}{letter_parts[1]}{num_chunks[0]:02d}"
                elif len(num_chunks) == 2 and 0 <= num_chunks[0] <= 9 and 0 <= num_chunks[1] <= 9:
                    return f"{letter_parts[0]}{letter_parts[1]}{num_chunks[0]}{num_chunks[1]}"

    # 3. Check purely numeric spoken input (ALL tokens must strictly parse as numbers)
    num_chunks = parse_number_tokens_strict(tokens)
    if num_chunks:
        # e.g. 4 single digits: [5, 8, 3, 1] or [7, 7, 7, 7]
        if len(num_chunks) == 4 and all(0 <= n <= 9 for n in num_chunks):
            return "".join(str(n) for n in num_chunks)
        # e.g. 2 pairs: [45, 45] or [77, 77]
        if len(num_chunks) == 2 and all(10 <= n <= 99 for n in num_chunks):
            return f"{num_chunks[0]}{num_chunks[1]}"
        # e.g. single 4-digit number: 5831, 7777
        if len(num_chunks) == 1 and 1000 <= num_chunks[0] <= 9999:
            return str(num_chunks[0])

    return None
