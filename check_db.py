import sqlite3
conn = sqlite3.connect('smart_money.db')
c = conn.cursor()

c.execute('SELECT COUNT(*) FROM positions')
total = c.fetchone()[0]
print(f'Всего позиций: {total}')

c.execute("SELECT COUNT(*) FROM positions WHERE status='CLOSED'")
closed = c.fetchone()[0]
print(f'Закрытых позиций: {closed}')

c.execute("SELECT COUNT(*) FROM positions WHERE status='OPEN'")
open_pos = c.fetchone()[0]
print(f'Открытых позиций: {open_pos}')

c.execute('SELECT id, symbol, side, entry_price, close_price, pnl, pnl_pct, status FROM positions ORDER BY id DESC LIMIT 10')
rows = c.fetchall()
print('\nПоследние 10 позиций:')
for r in rows:
    print(f'  {r}')

conn.close()
