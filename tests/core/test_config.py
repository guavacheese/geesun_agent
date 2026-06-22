from pydantic_settings import BaseSettings, SettingsConfigDict


class Test(BaseSettings):
    cube_api_key: str = "default"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


t = Test()
print(t.cube_api_key)  # 应该输出 e2b_dummy，而不是 dummy
