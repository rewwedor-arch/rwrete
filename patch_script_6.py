import re

with open('smart_money_aggressive.py', 'r', encoding='utf-8') as f:
    content = f.read()

def replace_position_size(match):
    return """            amount_usdt = min(amount_usdt, free_equity)
            if amount_usdt < config.MIN_SLOT_USDT:
                if free_equity >= config.MIN_SLOT_USDT:
                    amount_usdt = config.MIN_SLOT_USDT  # FIX: Set to MIN_SLOT, not ALL free equity!
                else:
                    return 0, 0, 0"""

content = re.sub(r'\s*amount_usdt = min\(amount_usdt, free_equity\)\s*if amount_usdt < config\.MIN_SLOT_USDT:\s*if free_equity >= config\.MIN_SLOT_USDT:\s*amount_usdt = free_equity\s*else:\s*return 0, 0, 0', replace_position_size, content, flags=re.DOTALL)

with open('smart_money_aggressive.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("Patch 6 applied successfully!")
