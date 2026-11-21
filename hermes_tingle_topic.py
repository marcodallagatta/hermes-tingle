"""Closed decorative topic icons. No inference, tools, storage or model calls."""
NEUTRAL = '📌'
ICONS = ('📌','📅','💶','📄','🩺','🧳','🪪','📦','🏠','🎓','💼','🚗','🎟️','🛒','🐾')


def safe_icon(value):
    return value if isinstance(value,str) and value in ICONS else NEUTRAL
