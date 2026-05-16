import sqlite3
from datetime import datetime, timezone

conn = sqlite3.connect('smart_money.db')
conn.row_factory = sqlite3.Row
c = conn.cursor()

# Все позиции
c.execute('SELECT * FROM positions ORDER BY id')
rows = c.fetchall()
print(f'Всего позиций в БД: {len(rows)}')

if rows:
    print('\nВсе позиции:')
    print(f'{"ID":>4} {"Symbol":<12} {"Side":<5} {"Entry":>10} {"Close":>10} {"PnL":>8} {"PnL%":>8} {"Status":<8}')
    print('-' * 70)
    for r in rows:
        entry = f'{r["entry_price"]:.4f}' if r['entry_price'] else '-'
        close = f'{r["close_price"]:.4f}' if r['close_price'] else '-'
        pnl = f'${r["pnl"]:.2f}' if r['pnl'] is not None else '-'
        pnl_pct = f'{r["pnl_pct"]:.1f}%' if r['pnl_pct'] is not None else '-'
        print(f'{r["id"]:>4} {r["symbol"]:<12} {r["side"]:<5} {entry:>10} {close:>10} {pnl:>8} {pnl_pct:>8} {r["status"]:<8}')

# Статистика по дням
c.execute('SELECT * FROM statistics ORDER BY date')
stats = c.fetchall()
print(f'\n\nСтатистика по дням:')
print(f'{"Date":<12} {"Trades":>6} {"Wins":>5} {"Loss":>5} {"PnL":>10} {"PnL%":>8}')
print('-' * 50)
for s in stats:
    print(f'{s["date"]:<12} {s["total_trades"]:>6} {s["profitable_trades"]:>5} {s["losing_trades"]:>5} ${s["total_pnl"]:>8.2f} {s["total_pnl_pct"]:>7.1f}%')

conn.close()
