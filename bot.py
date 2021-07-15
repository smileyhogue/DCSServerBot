# bot.py
import asyncio
import configparser
import discord
import logging
import os
import platform
import psycopg2
import psycopg2.extras
import shutil
from contextlib import closing, suppress
from discord.ext import commands
from logging.handlers import RotatingFileHandler
from os import path
from psycopg2 import pool

config = configparser.ConfigParser()
config.read('config/dcsserverbot.ini')

# Set the bot's version (not externally configurable)
VERSION = "1.1"

# git repository
GIT_REPO_URL = 'https://github.com/Special-K-s-Flightsim-Bots/DCSServerBot.git'

# COGs to load
COGS = ['cogs.master', 'cogs.statistics', 'cogs.help'] if config.getboolean('BOT', 'MASTER') is True else ['cogs.agent']

# Database Configuration
SQLITE_DATABASE = 'dcsserverbot.db'
TABLES_SQL = 'sql/tables.sql'
UPDATES_SQL = 'sql/update_{}.sql'
POOL_MIN = 5 if config.getboolean('BOT', 'MASTER') is True else 2
POOL_MAX = 10 if config.getboolean('BOT', 'MASTER') is True else 5


def get_prefix(client, message):
    prefixes = [config['BOT']['COMMAND_PREFIX']]
    # Allow users to @mention the bot instead of using a prefix
    return commands.when_mentioned_or(*prefixes)(client, message)


# Create the Bot
bot = commands.Bot(command_prefix=get_prefix,
                   description='Interact with DCS World servers',
                   owner_id=int(config['BOT']['OWNER']),
                   case_insensitive=True,
                   intents=discord.Intents.all())

# Allow COGs to access configuration
bot.config = config
bot.version = VERSION

# Initialize the logger and i18n
bot.log = logging.getLogger(name='dcsserverbot')
bot.log.setLevel(logging.DEBUG)
fh = RotatingFileHandler('dcsserverbot.log', maxBytes=10*1024*2024, backupCount=2)
fh.setLevel(logging.INFO)
fh.doRollover()
ch = logging.StreamHandler()
ch.setLevel(logging.WARN)
bot.log.addHandler(fh)
bot.log.addHandler(ch)

# List of DCS servers has to be global
bot.DCSServers = {}

# Autoupdate
if (config.getboolean('BOT', 'AUTOUPDATE') is True):
    try:
        import git

        try:
            with closing(git.Repo('.')) as repo:
                bot.log.info('Checking for updates...')
                current_hash = repo.head.commit.hexsha
                origin = repo.remotes.origin
                origin.fetch()
                new_hash = origin.refs[repo.active_branch.name].object.hexsha
                if (new_hash != current_hash):
                    restart = False
                    bot.log.warn('Remote repo has changed. Updating myself...')
                    diff = repo.head.commit.diff(new_hash)
                    for d in diff:
                        if (d.b_path == 'bot.py'):
                            restart = True
                    repo.remote().pull(repo.active_branch)
                    bot.log.warn('Updated to latest version.')
                    if (restart is True):
                        bot.log.warn('bot.py has changed. Restart needed.')
                        exit(-1)
                else:
                    bot.log.info('No update found.')
        except git.exc.InvalidGitRepositoryError:
            bot.log.warn('Linking bot to remote repository for auto update...')
            repo = git.Repo.init()
            origin = repo.create_remote('origin', url=GIT_REPO_URL)
            origin.fetch()
            repo.git.checkout('origin/master', '-f')
            bot.log.warn('Repository is linked. Restart needed.')
            exit(-1)

    except ImportError:
        bot.log.error('Autoupdate functionality requires "git" executable to be in the PATH.')
        exit(-1)


@bot.event
async def on_ready():
    bot.log.warning(f'Logged in as {bot.user.name} - {bot.user.id}')
    bot.remove_command('help')
    for cog in COGS:
        bot.load_extension(cog)
    return


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.NoPrivateMessage):
        await ctx.send('This command can\'t be used in a DM.')
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send('Parameter missing. Try !help')
    elif isinstance(error, commands.CommandNotFound):
        pass
    elif isinstance(error, commands.errors.CheckFailure):
        await ctx.send('You don\'t have the rights to use that command.')
    elif isinstance(error, asyncio.TimeoutError):
        await ctx.send('A timeout occured. Is the DCS server running?')
    else:
        await ctx.send(str(error))


@bot.event
async def on_message(message):
    for key, value in bot.DCSServers.items():
        if (value["chat_channel"] == message.channel.id):
            if (message.content.startswith(config['BOT']['COMMAND_PREFIX']) is False):
                message.content = config['BOT']['COMMAND_PREFIX'] + 'chat ' + message.content
    await bot.process_commands(message)


@bot.command(description='Reloads a COG', usage='<node> [cog]')
@commands.is_owner()
async def reload(ctx, node=platform.node(), cog=None):
    if (node == platform.node()):
        bot.config.read('config/dcsserverbot.ini')
        for c in COGS:
            if ((cog is None) or (c == cog)):
                bot.reload_extension(c)
        if (cog is None):
            await ctx.send('All COGs reloaded.')
        else:
            await ctx.send('COG {} reloaded.'.format(cog))

# Creating connection pool
bot.pool = pool.ThreadedConnectionPool(POOL_MIN, POOL_MAX, config['BOT']['DATABASE_URL'], sslmode='allow')
if (config.getboolean('BOT', 'MASTER') is True):
    # Initialize the database
    conn = bot.pool.getconn()
    try:
        with closing(conn.cursor()) as cursor:
            # check if there is a database already
            bot.db_version = None
            with suppress(Exception):
                cursor.execute('SELECT version FROM version')
                if (cursor.rowcount == 1):
                    bot.db_version = cursor.fetchone()[0]
                    while (path.exists(UPDATES_SQL.format(bot.db_version))):
                        bot.log.warning('Upgrading Database version {} ...'.format(bot.db_version))
                        with open(UPDATES_SQL.format(bot.db_version)) as tables_sql:
                            for query in tables_sql.readlines():
                                bot.log.debug(query.rstrip())
                                cursor.execute(query.rstrip())
                        cursor.execute('SELECT version FROM version')
                        bot.db_version = cursor.fetchone()[0]
                        bot.log.warning('Database upgraded to version {}.'.format(bot.db_version))
            # no, create one
            if (bot.db_version is None):
                bot.log.warning('Initializing Database ...')
                with open(TABLES_SQL) as tables_sql:
                    for query in tables_sql.readlines():
                        bot.log.debug(query.rstrip())
                        cursor.execute(query.rstrip())
                bot.log.warning('Database initialized.')
            conn.commit()
    except (Exception, psycopg2.DatabaseError) as error:
        conn.rollback()
        bot.log.exception(error)
        exit(-1)
    finally:
        bot.pool.putconn(conn)

# Installing Hook
dcs_path = os.path.expandvars(config['DCS']['DCS_HOME'] + '\\Scripts')
assert path.exists(dcs_path), 'Can\'t find DCS installation directory. Exiting.'
ignore = None
if (path.exists(dcs_path + '\\net\\DCSServerBot')):
    bot.log.info('Updating Hook ...')
    ignore = shutil.ignore_patterns('DCSServerBotConfig.lua')
else:
    bot.log.info('Installing Hook ...')
shutil.copytree('./Scripts', dcs_path, dirs_exist_ok=True, ignore=ignore)
bot.log.info('Hook installed.')

# TODO change sanitizeModules
bot.log.warning('Starting {}-Node on {}'.format('Master' if config.getboolean(
    'BOT', 'MASTER') is True else 'Agent', platform.node()))
bot.run(config['BOT']['TOKEN'], bot=True, reconnect=True)
