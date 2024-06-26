import logging
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from pydantic import BaseSettings


logger = logging.getLogger(__name__)


dotenv_path = Path('../.env' if '\\tests' in os.getcwd() else
                   '../../.env' if '\\generate_data' in os.getcwd() else
                   '.env')
load_dotenv(dotenv_path=dotenv_path)


def check_envvar_is_not_none(var_name: Optional[str]):
    if var_name is not None:
        return var_name
    else:
        logger.error('Missing required environment variable, setting value to None.')
        raise TypeError('Required environment variable is None, should be string.')


class Settings(BaseSettings):
    DB_USER: str = check_envvar_is_not_none(os.getenv('PG_USER'))
    DB_PASSWORD: str = check_envvar_is_not_none(os.getenv('PG_PASSWORD'))
    DB_HOST: str = check_envvar_is_not_none(os.getenv('PG_HOST'))
    DB_DATABASE: str = check_envvar_is_not_none(os.getenv('PG_DATABASE'))
    DB_DATABASE_TEST: Optional[str] = os.getenv('PG_DATABASE_TEST')
    # Path to glpk executable. Probably ends with \w64\glpsol
    GLPK_PATH: Optional[str] = os.getenv('GLPK_PATH')
    # Whether to run in "test mode", simulating only a few days of the year.
    NOT_FULL_YEAR: bool = os.getenv('NOT_FULL_YEAR', 'False').lower() in ('true', '1', 't')


settings = Settings()
