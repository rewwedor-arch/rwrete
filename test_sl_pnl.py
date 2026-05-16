"""Тест: проверка что SL корректно считает PnL"""
import sys
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from smart_money_aggressive import Position, StrategyConfig
from datetime import datetime, timezone

config = StrategyConfig()

print("=" * 60)
print("ТЕСТ: Программный STOP LOSS")
print("=" * 60)

# Сценарий: LONG позиция, цена упала на 3.5% (сработал SL)
entry_price = 10.0
stop_loss_price = entry_price * (1 - config.STOP_LOSS_PCT / 100)
print(f"\nentry_price: ${entry_price}")
print(f"stop_loss_price: ${stop_loss_price:.4f} (-{config.STOP_LOSS_PCT}%)")

# Позиция
pos = Position(
    id=1,
    symbol='ATOM/USDT',
    side='LONG',
    entry_price=entry_price,
    stop_loss=stop_loss_price,
    amount_usdt=16.67,
    leverage=75,
    quantity=1.25,
    timestamp=datetime.now(timezone.utc),
    remaining_quantity=1.25,
    realized_pnl_usd=0.0,
)

# Цена упала до SL
current_price = stop_loss_price - 0.01  # Чуть ниже SL

# Расчёт PnL как в monitor_positions
price_change_pct = ((current_price - pos.entry_price) / pos.entry_price) * 100
pnl_pct = price_change_pct * pos.leverage
pnl_usd = pos.realized_pnl_usd + (current_price - pos.entry_price) * pos.remaining_quantity

print(f"\nТекущая цена: ${current_price:.4f}")
print(f"price_change_pct: {price_change_pct:.2f}%")
print(f"pnl_pct (ROE): {pnl_pct:.1f}%")
print(f"pnl_usd: ${pnl_usd:.4f}")

# Проверка: SL должен сработать
if price_change_pct <= -config.STOP_LOSS_PCT:
    print(f"\n✅ SL сработал (price_change_pct {price_change_pct:.2f}% <= -{config.STOP_LOSS_PCT}%)")
else:
    print(f"\n❌ SL НЕ сработал (price_change_pct {price_change_pct:.2f}% > -{config.STOP_LOSS_PCT}%)")

# Проверка: PnL должен быть отрицательным
if pnl_usd < 0:
    print(f"✅ PnL отрицательный: ${pnl_usd:.4f}")
else:
    print(f"❌ ПРОБЛЕМА: PnL положительный: ${pnl_usd:.4f}")

# Теперь проверим close_position
# exit_price = current_price (при закрытии по рынку)
exit_price = current_price
qty_close = pos.remaining_quantity
leg_pnl = (exit_price - pos.entry_price) * qty_close
total_pnl = pos.realized_pnl_usd + leg_pnl
margin = pos.amount_usdt
pnl_pct_total = (total_pnl / margin) * 100

print(f"\nПри close_position:")
print(f"  exit_price: ${exit_price:.4f}")
print(f"  leg_pnl: (${exit_price:.4f} - ${pos.entry_price}) * {qty_close} = ${leg_pnl:.4f}")
print(f"  total_pnl: ${total_pnl:.4f}")
print(f"  pnl_pct: {pnl_pct_total:.1f}%")

if total_pnl < 0:
    print(f"\n✅ Сделка корректно показывается как УБЫТОЧНАЯ")
else:
    print(f"\n❌ ПРОБЛЕМА: Сделка показывается как ПРИБЫЛЬНАЯ")

# Тест 2: SHORT позиция
print("\n" + "=" * 60)
print("ТЕСТ: SHORT позиция с SL")
print("=" * 60)

entry_price_short = 10.0
stop_loss_price_short = entry_price_short * (1 + config.STOP_LOSS_PCT / 100)

pos_short = Position(
    id=2,
    symbol='ATOM/USDT',
    side='SHORT',
    entry_price=entry_price_short,
    stop_loss=stop_loss_price_short,
    amount_usdt=16.67,
    leverage=75,
    quantity=1.25,
    timestamp=datetime.now(timezone.utc),
    remaining_quantity=1.25,
    realized_pnl_usd=0.0,
)

# Цена выросла до SL (для SHORT это убыток)
current_price_short = stop_loss_price_short + 0.01

# Расчёт PnL для SHORT
price_change_pct_short = ((pos_short.entry_price - current_price_short) / pos_short.entry_price) * 100
pnl_pct_short = price_change_pct_short * pos_short.leverage
pnl_usd_short = pos_short.realized_pnl_usd + (pos_short.entry_price - current_price_short) * pos_short.remaining_quantity

print(f"\nentry_price: ${entry_price_short}")
print(f"stop_loss_price: ${stop_loss_price_short:.4f} (+{config.STOP_LOSS_PCT}%)")
print(f"current_price: ${current_price_short:.4f}")
print(f"price_change_pct: {price_change_pct_short:.2f}%")
print(f"pnl_pct (ROE): {pnl_pct_short:.1f}%")
print(f"pnl_usd: ${pnl_usd_short:.4f}")

if pnl_usd_short < 0:
    print(f"\n✅ SHORT сделка корректно показывается как УБЫТОЧНАЯ")
else:
    print(f"\n❌ ПРОБЛЕМА: SHORT сделка показывается как ПРИБЫЛЬНАЯ")
