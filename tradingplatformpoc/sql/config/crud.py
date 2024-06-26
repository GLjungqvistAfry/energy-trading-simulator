import logging
from collections import Counter
from contextlib import _GeneratorContextManager
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from sqlalchemy import select

from sqlmodel import Session, exists

from tradingplatformpoc.config.screen_config import param_diff
from tradingplatformpoc.connection import session_scope
from tradingplatformpoc.sql.agent.crud import create_agent_if_not_in_db
from tradingplatformpoc.sql.agent.models import Agent as TableAgent
from tradingplatformpoc.sql.config.models import Config, ConfigCreate
from tradingplatformpoc.sql.job.models import Job

logger = logging.getLogger(__name__)


def create_config_if_not_in_db(config: dict, config_id: str, description: str) -> dict:
    # TODO validate ID
    id_exists = check_if_id_in_db(config_id=config_id)
    if id_exists is not None:
        logger.warning('Configuration ID {} already exists in database.'.format(id_exists))
        return {'created': False, 'id': id_exists,
                'message': 'Configuration ID {} already exists in database.'.format(id_exists)}

    agent_name_and_ids = {agent['Name']: create_agent_if_not_in_db(agent) for agent in config['Agents'][:]}

    # Check if matching config exists already
    config_exists_id = check_if_config_in_db(config=config, agent_ids=list(agent_name_and_ids.values()))
    if config_exists_id is None:
        db_config_id = create_config(ConfigCreate(id=config_id,
                                                  description=description,
                                                  agents_spec=agent_name_and_ids,
                                                  area_info=config['AreaInfo'],
                                                  mock_data_constants=config['MockDataConstants']))
        logger.info('Configuration with ID {} created!'.format(db_config_id))
        return {'created': True, 'id': db_config_id, 'message': 'Config created with ID {}'.format(db_config_id)}
    else:
        message = 'Configuration already exists in database with ID {}'.format(config_exists_id)
        logger.warning(message)
        return {'created': False, 'id': config_exists_id, 'message': message}


def create_config(config: ConfigCreate,
                  session_generator: Callable[[], _GeneratorContextManager[Session]] = session_scope) -> str:
    with session_generator() as db:
        config_to_db = Config.from_orm(config)
        db.add(config_to_db)
        db.commit()
        db.refresh(config_to_db)
        return config_to_db.id


def check_if_config_in_db(config: dict, agent_ids: List[str],
                          session_generator: Callable[[], _GeneratorContextManager[Session]] = session_scope) -> \
        Optional[str]:
    with session_generator() as db:
        configs_in_db = db.execute(select(Config)).all()
        for (config_in_db,) in configs_in_db:
            config_in_db_params = {'AreaInfo': config_in_db.area_info,
                                   'MockDataConstants': config_in_db.mock_data_constants}
            new_params = {'AreaInfo': config['AreaInfo'],
                          'MockDataConstants': config['MockDataConstants']}
            changed_area_info_params, changed_mock_data_params = param_diff(config_in_db_params, new_params)
            if (len(changed_area_info_params) == 0) and (len(changed_mock_data_params) == 0):
                # General parameters were all the same. Compare agents:
                diff1 = Counter(config_in_db.agents_spec.values()) - Counter(agent_ids)
                diff2 = Counter(agent_ids) - Counter(config_in_db.agents_spec.values())

                if (len(diff1) == 0) & (len(diff2) == 0):
                    return config_in_db.id
        return None


def read_config(config_id: str,
                session_generator: Callable[[], _GeneratorContextManager[Session]] = session_scope) -> \
        Optional[Dict[str, Any]]:
    # TODO: Handle config not found
    with session_generator() as db:
        config = db.get(Config, config_id)
        if config is not None:
            res = db.execute(select(TableAgent).where(TableAgent.id.in_(config.agents_spec.values()))).all()
            agents = [{'Name': [name for name, aid in config.agents_spec.items() if aid == agent.id][0],
                       'Type': agent.agent_type, **agent.agent_config} for (agent,) in res]
            return {'Agents': agents, 'AreaInfo': config.area_info,
                    'MockDataConstants': config.mock_data_constants}
        else:
            logger.error('Configuration with ID {} not found.'.format(config_id))
            return None
        

def read_description(config_id: str,
                     session_generator: Callable[[], _GeneratorContextManager[Session]] = session_scope) -> \
        Optional[str]:
    with session_generator() as db:
        res = db.execute(select(Config.description).where(Config.id == config_id)).first()
        return res[0] if res is not None else None


def get_all_config_ids_in_db_without_jobs(session_generator: Callable[[], _GeneratorContextManager[Session]]
                                          = session_scope) -> List[str]:
    with session_generator() as db:
        res = db.query(Config.id).filter(~exists().where(Job.config_id == Config.id))
        return [config_id for (config_id,) in res]


def get_all_finished_job_config_id_pairs_in_db(session_generator: Callable[[], _GeneratorContextManager[Session]]
                                               = session_scope) -> Dict[str, str]:
    with session_generator() as db:
        res = db.execute(select(Config.id.label('config_id'), Job.id.label('job_id'))
                         .join(Config, Job.config_id == Config.id).where(Job.end_time.is_not(None))
                         .order_by(Job.start_time.desc())).all()
        return {elem.config_id: elem.job_id for elem in res}


def get_all_config_ids_in_db_with_jobs_df(session_generator: Callable[[], _GeneratorContextManager[Session]]
                                          = session_scope) -> pd.DataFrame:
    with session_generator() as db:
        res = db.execute(select(Job, Config.description).
                         join(Config, Job.config_id == Config.id).
                         where(Job.fail_info.is_(None))).all()
        return pd.DataFrame.from_records([{'Job ID': job.id, 'Config ID': job.config_id, 'Description': desc,
                                           'Start time': job.start_time, 'End time': job.end_time}
                                         for (job, desc) in res])


def get_all_configs_in_db_df(session_generator: Callable[[], _GeneratorContextManager[Session]]
                             = session_scope) -> pd.DataFrame:
    with session_generator() as db:
        res = db.execute(select(Config)).all()
        return pd.DataFrame.from_records([{'Config ID': config.id, 'Description': config.description}
                                          for (config,) in res])


def get_all_config_ids_in_db(session_generator: Callable[[], _GeneratorContextManager[Session]]
                             = session_scope) -> List[str]:
    with session_generator() as db:
        res = db.execute(select(Config.id).outerjoin(Job, Job.config_id == Config.id)).all()
        return [config_id for (config_id,) in res]


def check_if_id_in_db(config_id: str,
                      session_generator: Callable[[], _GeneratorContextManager[Session]]
                      = session_scope) -> Optional[str]:
    with session_generator() as db:
        res = db.execute(select(Config.id).where(Config.id == config_id)).first()
        return res[0] if res is not None else None

 
def get_all_agent_name_id_pairs_in_config(config_id: str,
                                          session_generator: Callable[[], _GeneratorContextManager[Session]]
                                          = session_scope) -> Dict[str, str]:
    """Returns agent's names and ids."""
    with session_generator() as db:
        res = db.execute(select(Config.agents_spec).where(Config.id == config_id)).first()
        if res is None:
            raise RuntimeError("No agents found for config with ID '{}'".format(config_id))
        return res[0]


def delete_config(config_id: str,
                  session_generator: Callable[[], _GeneratorContextManager[Session]]
                  = session_scope) -> bool:
    with session_generator() as db:
        config = db.get(Config, config_id)
        if not config:
            logger.error('No config in database with ID {}'.format(config_id))
            return False
        else:
            db.delete(config)
            db.commit()
            logger.info('Configuration with ID {} deleted'.format(config_id))
            return True


def get_job_ids_for_config_id(config_id: str,
                              session_generator: Callable[[], _GeneratorContextManager[Session]]
                              = session_scope) -> List[str]:
    with session_generator() as db:
        res = db.execute(select(Job.id).where(Job.config_id == config_id)).all()
        return [job_id for (job_id,) in res]


def delete_config_if_no_jobs_exist(config_id: str) -> bool:
    job_ids = get_job_ids_for_config_id(config_id)
    if len(job_ids) == 0:
        return delete_config(config_id)
    else:
        logger.error('Cannot delete configuration with existing runs saved.')
        return False


def get_mock_data_constants(config_id: str,
                            session_generator: Callable[[], _GeneratorContextManager[Session]]
                            = session_scope) -> Optional[Dict[str, Any]]:
    with session_generator() as db:
        res = db.execute(select(Config.mock_data_constants).where(Config.id == config_id)).first()
        return res[0] if res is not None else None
    

def update_description(config_id: str, new_description: str,
                       session_generator: Callable[[], _GeneratorContextManager[Session]]
                       = session_scope):
    with session_generator() as db:
        db.query(Config).filter(Config.id == config_id).update({'description': new_description})
        logger.info('Updated description for config ID {}'.format(config_id))
