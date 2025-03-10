import asyncio
import psycopg2
from contextlib import closing
from core import utils
from core.report.errors import ValueNotInRange
from typing import Any, List, Tuple


def parse_params(kwargs: dict, params: Tuple[dict, List]):
    new_args = kwargs.copy()
    if isinstance(params, dict):
        for key, value in params.items():
            new_args[key] = value
    else:
        new_args['params'] = params
    return new_args


async def parse_input(self, kwargs: dict, params: List[Any]):
    new_args = kwargs.copy()
    for param in params:
        if 'name' in param:
            if param['name'] in new_args and new_args[param['name']]:
                if 'range' in param:
                    value = new_args[param['name']] or ''
                    if value not in param['range']:
                        raise ValueNotInRange(param['name'], value, param['range'])
                elif 'value' in param:
                    value = param['value']
                    new_args[param['name']] = utils.format_string(value, '_ _', **kwargs) if isinstance(value, str) else value
            elif 'value' in param:
                value = param['value']
                new_args[param['name']] = utils.format_string(value, '_ _', **kwargs) if isinstance(value, str) else value
            elif 'default' in param:
                new_args[param['name']] = param['default']
        elif 'sql' in param:
            conn = self.pool.getconn()
            try:
                with closing(conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)) as cursor:
                    cursor.execute(utils.format_string(param['sql'], **kwargs), kwargs)
                    if cursor.rowcount == 1:
                        for name, value in cursor.fetchone().items():
                            new_args[name] = value
            except psycopg2.DatabaseError as error:
                self.log.exception(error)
                raise
            finally:
                self.pool.putconn(conn)
        elif 'callback' in param:
            try:
                data: dict = await kwargs['server'].sendtoDCSSync({
                    "command": "getVariable", "name": param['callback']
                })
                if 'value' in data:
                    new_args[param['callback']] = data['value']
            except asyncio.TimeoutError:
                new_args[param['callback']] = None
    return new_args
