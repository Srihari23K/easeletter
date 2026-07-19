import re

file_path = "engine/template_engine.py"

with open(file_path, 'r', encoding='utf-8') as file:
    content = file.read()

fixed_content = re.sub(r'\{\{(.*?)\}\}', r'{\1}', content)

with open(file_path, 'w', encoding='utf-8') as file:
    file.write(fixed_content)

print("✅ Templates updated from {{variable}} to {variable}")