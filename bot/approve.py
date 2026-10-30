"""Совместимость: одобрение карточек живёт в bot.cards (5.1), здесь только реэкспорт."""
from bot.cards import approve, is_card, on_callback, on_rollback, post_card, process_proposed

__all__ = ["approve", "is_card", "on_callback", "on_rollback", "post_card", "process_proposed"]
