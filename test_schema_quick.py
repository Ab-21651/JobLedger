from jobledger.db.schema import init_db
conn = init_db('test_schema.db')
print('Schema OK')
tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
print('Tables:', tables)
conn.close()
import os; os.remove('test_schema.db')
