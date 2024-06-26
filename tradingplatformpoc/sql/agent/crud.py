import logging
from contextlib import _GeneratorContextManager
from typing import Any, Callable, Dict, List

from sqlalchemy import select

from sqlmodel import Session

from tradingplatformpoc.connection import session_scope
from tradingplatformpoc.sql.agent.models import Agent, AgentCreate

logger = logging.getLogger(__name__)


def create_agent_if_not_in_db(agent: Dict[str, Any]):
    """
    If agent with config not already in db, insert
    """
    agent_config = {key: val for key, val in agent.items() if key not in ['Name', 'Type']}
    agent_type = agent['Type']
    agent_in_db_id = check_if_agent_in_db(agent_type, agent_config)
    if agent_in_db_id is None:
        agent_in_db_id = create_agent(AgentCreate(agent_config=agent_config, agent_type=agent_type))
        logger.info('Agent created with id {}'.format(agent_in_db_id))
    return agent_in_db_id


def create_agent(agent: AgentCreate,
                 session_generator: Callable[[], _GeneratorContextManager[Session]] = session_scope):
    with session_generator() as db:
        agent_to_db = Agent.from_orm(agent)
        db.add(agent_to_db)
        db.commit()
        db.refresh(agent_to_db)
        return agent_to_db.id


def check_if_agent_in_db(agent_type: str, agent_config: Dict[str, Any],
                         session_generator: Callable[[], _GeneratorContextManager[Session]] = session_scope):
    with session_generator() as db:
        agents_in_db = db.execute(select(Agent.id.label('id'),
                                         Agent.agent_config.label('config'))
                                  .where(Agent.agent_type == agent_type)).all()
        for agent_in_db in agents_in_db:
            symmetric_diff = set(agent_config.items()).symmetric_difference(set(agent_in_db.config.items()))
            if len(symmetric_diff) == 0:
                logger.debug('Agent found in db with id {}'.format(agent_in_db.id))
                return agent_in_db.id
            
        return None


def get_block_agent_dicts_from_id_list(
        agent_ids: List[str],
        session_generator: Callable[[], _GeneratorContextManager[Session]]
        = session_scope) -> List[Dict[str, Any]]:
    """
    Get agents
    """
    with session_generator() as db:
        res = db.query(Agent).filter(Agent.id.in_(agent_ids),
                                     Agent.agent_type == 'BlockAgent').all()
        return [{'db_id': agent.id, 'Type': agent.agent_type, **agent.agent_config}
                for agent in res]
    

def get_agent_type(agent_id: str,
                   session_generator: Callable[[], _GeneratorContextManager[Session]] = session_scope):
    with session_generator() as db:
        res = db.execute(select(Agent.agent_type).where(Agent.id == agent_id)).first()
        return res[0] if res is not None else None
    

def get_agent_config(agent_id: str,
                     session_generator: Callable[[], _GeneratorContextManager[Session]] = session_scope):
    with session_generator() as db:
        res = db.execute(select(Agent.agent_config).where(Agent.id == agent_id)).first()
        return res[0] if res is not None else None
