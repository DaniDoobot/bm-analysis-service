"""Visual formatters for mass evaluation results."""
from typing import Any

def build_items_visual(items_json: Any) -> list[dict[str, Any]]:
    """
    Format items_json for visualization.
    Returns:
    [
      {
        "label": "Empatía",
        "type": "score_1_10",
        "value": 7,
        "display_value": "7/10",
        "feedback": "...",
        "output_key": "empatia",
        "criterion_key": "empatia",
        "category": "Puntuación 1-10"
      }
    ]
    """
    visuals = []
    if not isinstance(items_json, list):
        return visuals
        
    category_map = {
        "score_1_10": "Puntuación 1-10",
        "boolean": "Sí / No",
        "percentage": "Porcentaje",
        "number": "Numérico",
        "text": "Texto libre",
        "category": "Categoría"
    }
    
    for item in items_json:
        if not isinstance(item, dict):
            continue
        key = item.get("criterion_key") or item.get("output_key") or "unknown"
        name = item.get("name") or key
        val = item.get("value")
        c_type = item.get("type") or item.get("criterion_type") or "text"
        is_na = item.get("not_applicable") is True
        display_value = "N/A" if is_na else "Sin dato"
        if not is_na and val is not None and str(val).strip().lower() not in ["null", "none", ""]:
            if c_type == "score_1_10":
                try:
                    f_val = float(val)
                    if f_val.is_integer():
                        display_value = f"{int(f_val)}/10"
                    else:
                        display_value = f"{f_val}/10"
                except (ValueError, TypeError):
                    display_value = f"{val}/10"
            elif c_type == "boolean":
                if isinstance(val, bool):
                    display_value = "Sí" if val else "No"
                elif str(val).strip().lower() in ["true", "1", "yes", "sí", "si"]:
                    display_value = "Sí"
                elif str(val).strip().lower() in ["false", "0", "no"]:
                    display_value = "No"
                else:
                    display_value = str(val)
            elif c_type == "percentage":
                val_str = str(val).strip()
                if val_str.endswith("%"):
                    display_value = val_str
                else:
                    try:
                        f_val = float(val)
                        if f_val.is_integer():
                            display_value = f"{int(f_val)}%"
                        else:
                            display_value = f"{f_val}%"
                    except (ValueError, TypeError):
                        display_value = f"{val_str}%"
            elif c_type == "number":
                try:
                    f_val = float(val)
                    if f_val.is_integer():
                        display_value = str(int(f_val))
                    else:
                        display_value = str(f_val)
                except (ValueError, TypeError):
                    display_value = str(val)
            else:  # text or category
                display_value = str(val)
                
        visuals.append({
            "label": name,
            "type": c_type,
            "value": val,
            "display_value": display_value,
            "feedback": item.get("feed") or item.get("comment") or item.get("feedback") or "",
            "output_key": item.get("output_key") or key,
            "criterion_key": key,
            "category": category_map.get(c_type, "Texto libre"),
            "not_applicable": is_na
        })
    return visuals
