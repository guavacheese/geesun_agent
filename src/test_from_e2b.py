import os

import logging

logging.basicConfig(level=logging.DEBUG)

from e2b_code_interpreter import Sandbox


with Sandbox.create(
    template="tpl-feb83bbc69ae4fb897329c54",
    api_key="dummy",
    api_url="http://192.168.10.136:13000",
    secure=False,
) as sb:
    result = sb.run_code("print('============hello')")
    print(result)
