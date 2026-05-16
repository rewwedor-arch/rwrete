"""Тест: проверка PnL при частичных закрытиях"""
import sys
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from smart_money_aggressive import Position
from datetime import datetime, timezone

print("=" * 60)
print("ТЕСТ: Частичные закрытия - сценарий 'все сделки прибыльные'")
print("=" * 60)

# Сценарий: Бот открывает позицию, цена немного растёт,
# бот делает частичное закрытие в плюс, потом цена падает,
# но total_pnl всё равно положительный из-за realized_pnl_usd

# Создаём позицию
pos = Position(
    id=1,
    symbol='ATOM/USDT',
    side='LONG',
    entry_price=10.0,
    stop_loss=9.65,
    amount_usdt=16.67,  # Маржа
    leverage=75,
    quantity=1.25,  # Номинал = 1.25 * 10 = $12.5
    timestamp=datetime.now(timezone.utc),
    remaining_quantity=1.25,
    realized_pnl_usd=0.0,
)

print(f"\nНачальная позиция:")
print(f"  entry_price: ${pos.entry_price}")
print(f"  quantity: {pos.quantity}")
print(f"  margin: ${pos.amount_usdt}")
print(f"  leverage: x{pos.leverage}")

# Частичное закрытие TP1: закрываем 30% позиции при цене $10.50
# PnL на эту часть: (10.50 - 10.00) * (1.25 * 0.30) = 0.50 * 0.375 = $0.1875
partial_qty = pos.quantity * 0.30
partial_price = 10.50
partial_pnl = (partial_price - pos.entry_price) * partial_qty
pos.realized_pnl_usd += partial_pnl
pos.remaining_quantity -= partial_qty

print(f"\nПосле частичного закрытия TP1 (30% при ${partial_price}):")
print(f"  Закрыто: {partial_qty:.4f} монет")
print(f"  PnL на эту часть: ${partial_pnl:.4f}")
print(f"  realized_pnl_usd: ${pos.realized_pnl_usd:.4f}")
print(f"  remaining_quantity: {pos.remaining_quantity:.4f}")

# Теперь цена падает до $9.00 (ниже входа!)
# Закрываем остаток
exit_price = 9.00
leg_pnl = (exit_price - pos.entry_price) * pos.remaining_quantity
total_pnl = pos.realized_pnl_usd + leg_pnl
margin = pos.amount_usdt
pnl_pct = (total_pnl / margin) * 100

print(f"\nЗакрытие остатка при ${exit_price}:")
print(f"  leg_pnl: (${exit_price} - ${pos.entry_price}) * {pos.remaining_quantity:.4f} = ${leg_pnl:.4f}")
print(f"  total_pnl: ${pos.realized_pnl_usd:.4f} + (${leg_pnl:.4f}) = ${total_pnl:.4f}")
print(f"  pnl_pct: {pnl_pct:.1f}%")

if total_pnl >= 0:
    print(f"\n  ⚠️ ПРОБЛЕМА: Сделка показывается как ПРИБЫЛЬНАЯ (+${total_pnl:.4f})")
    print(f"  Но по факту: вошли в $10, вышли в $9 — это УБЫТОК!")
else:
    print(f"\n  ✅ Сделка корректно показывается как УБЫТОЧНАЯ (${total_pnl:.4f})")

# Проверка: реальный убыток или прибыль?
# Вложили: $16.67 маржи
# Номинал: 1.25 * $10 = $12.5
# Если закрыть всё при $9: PnL = (9 - 10) * 1.25 = -$1.25
# Но мы частично закрыли при $10.50: +$0.1875
# Итого: -$1.25 + $0.1875 = -$1.0625 (УБЫТОК!)

expected_total_pnl = (exit_price - pos.entry_price) * pos.quantity  # Полный PnL если бы закрыли всё сразу
print(f"\n  Для сравнения: если бы закрыли всё при ${exit_price}:")
print(f"  PnL = (${exit_price} - ${pos.entry_price}) * {pos.quantity} = ${expected_total_pnl:.4f}")

# Проверка: при каком exit_price total_pnl станет положительным?
# total_pnl = realized_pnl_usd + (exit - entry) * remaining = 0
# 0.1875 + (exit - 10) * 0.875 = 0
# (exit - 10) * 0.875 = -0.1875
# exit - 10 = -0.2143
# exit = 9.7857
breakeven_price = pos.entry_price - (pos.realized_pnl_usd / pos.remaining_quantity)
print(f"\n  Точка безубыточности для остатка: ${breakeven_price:.4f}")
print(f"  При закрытии выше ${breakeven_price:.4f} — будет прибыль")
print(f"  При закрытии ниже ${breakeven_price:.4f} — будет убыток")

if exit_price < breakeven_price:
    print(f"\n  ⚠️ exit_price (${exit_price}) < breakeven (${breakeven_price:.4f})")
    print(f"  СДЕЛКА ДОЛЖНА БЫТЬ УБЫТОЧНОЙ!")
    if total_pnl >= 0:
        print(f"  БАГ: total_pnl = ${total_pnl:.4f} (положительный)")
    else:
        print(f"  OK: total_pnl = ${total_pnl:.4f} (отрицательный)")
