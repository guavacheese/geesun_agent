from langchain_cubesandbox.sandbox import CubeSandbox

s = CubeSandbox(
    template="tpl-feb83bbc69ae4fb897329c54",
    api_url="http://192.168.10.136:13000",
    api_key="dummy",
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
