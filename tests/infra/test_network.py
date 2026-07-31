from langchain_cubesandbox.sandbox import CubeSandbox
import os
from dotenv import load_dotenv

load_dotenv()

s = CubeSandbox(
    template=os.environ["CUBE_TEMPLATE_ID"],
    api_key=os.environ["CUBE_API_KEY"],
    api_url=os.environ["CUBE_API_URL"],
)

print("Sandbox ID:", s.id)

# 1. 基本 echo
r1 = s._sandbox.commands.run("echo hello")
print("echo:", r1)

# 2. python3
r2 = s._sandbox.commands.run('python3 -c "print(42)"')
print("python3:", r2)

# 3. refresh_timeout
try:
    s.refresh_timeout()
    print("refresh_timeout OK")
except Exception as e:
    print(f"refresh_timeout ERR: {e}")

s.close()
