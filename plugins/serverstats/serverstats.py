import numpy as np
import pandas as pd
import psycopg2
from contextlib import closing
from core import const, report
from matplotlib.ticker import FuncFormatter
from typing import Optional


class ServerUsage(report.EmbedElement):

    def render(self, server_name: Optional[str], period: Optional[str]):
        sql = f"SELECT trim(regexp_replace(m.server_name, '{self.bot.config['FILTER']['SERVER_FILTER']}', '', 'g')) " \
              f"AS server_name, ROUND(SUM(EXTRACT(EPOCH FROM (s.hop_off - s.hop_on))) / 3600) AS playtime, " \
              f"COUNT(DISTINCT s.player_ucid) AS players, COUNT(DISTINCT p.discord_id) AS members FROM missions m, " \
              f"statistics s, players p WHERE m.id = s.mission_id AND s.player_ucid = p.ucid AND s.hop_off IS NOT NULL"
        if server_name:
            sql += f' AND m.server_name = %s'
        if period:
            sql += f" AND DATE(s.hop_on) > (DATE(NOW()) - interval '1 {period}')"
        sql += ' GROUP BY 1 ORDER BY 2 DESC'

        conn = self.pool.getconn()
        try:
            with closing(conn.cursor(cursor_factory=psycopg2.extras.DictCursor)) as cursor:
                servers = playtimes = players = members = ''
                cursor.execute(sql, (server_name, ))
                for row in cursor.fetchall():
                    servers += row['server_name'] + '\n'
                    playtimes += '{:.0f}\n'.format(row['playtime'])
                    players += '{:.0f}\n'.format(row['players'])
                    members += '{:.0f}\n'.format(row['members'])
                if len(servers) > 0:
                    if not server_name:
                        self.add_field(name='Server', value=servers)
                    self.add_field(name='Playtime (h)', value=playtimes)
                    self.add_field(name='Unique Players', value=players)
                    if server_name:
                        self.add_field(name='Discord Members', value=members)
        except (Exception, psycopg2.DatabaseError) as error:
            self.log.exception(error)
        finally:
            self.pool.putconn(conn)


class TopMissionPerServer(report.EmbedElement):

    def render(self, server_name: Optional[str], period: Optional[str], limit: int):
        sql_left = 'SELECT server_name, mission_name, playtime FROM (SELECT server_name, ' \
                                      'mission_name, playtime, ROW_NUMBER() OVER(PARTITION BY server_name ORDER BY ' \
                                      'playtime DESC) AS rn FROM ( '
        sql_inner = f"SELECT trim(regexp_replace(m.server_name, '{self.bot.config['FILTER']['SERVER_FILTER']}', '', " \
                    f"'g')) AS server_name, trim(regexp_replace(m.mission_name, " \
                    f"'{self.bot.config['FILTER']['MISSION_FILTER']}', ' ', 'g')) AS mission_name, " \
                    f"ROUND(SUM(EXTRACT(EPOCH FROM (s.hop_off - s.hop_on))) / 3600) AS playtime FROM missions m, " \
                    f"statistics s WHERE m.id = s.mission_id AND s.hop_off IS NOT NULL"
        sql_right = ') AS x) AS y WHERE rn {} ORDER BY 3 DESC'
        if server_name:
            sql_inner += f' AND m.server_name = %s'
        if period:
            sql_inner += f" AND DATE(s.hop_on) > (DATE(NOW()) - interval '1 {period}')"
        sql_inner += ' GROUP BY 1, 2'

        conn = self.pool.getconn()
        try:
            with closing(conn.cursor(cursor_factory=psycopg2.extras.DictCursor)) as cursor:
                servers = missions = playtimes = ''
                cursor.execute(sql_left + sql_inner + sql_right.format(
                    '= 1' if not server_name else f'<= {limit}'), (server_name, ))
                for row in cursor.fetchall():
                    servers += row['server_name'] + '\n'
                    missions += row['mission_name'][:20] + '\n'
                    playtimes += '{:.0f}\n'.format(row['playtime'])
                if len(servers) > 0:
                    if not server_name:
                        self.add_field(name='Server', value=servers)
                    self.add_field(name='TOP Mission' if not server_name else f"TOP {limit} Missions", value=missions)
                    self.add_field(name='Playtime (h)', value=playtimes)
                    if server_name:
                        self.add_field(name='_ _', value='_ _')
        except (Exception, psycopg2.DatabaseError) as error:
            self.log.exception(error)
        finally:
            self.pool.putconn(conn)


class TopModulesPerServer(report.EmbedElement):

    def render(self, server_name: Optional[str], period: Optional[str], limit: int):
        sql = 'SELECT s.slot, COUNT(s.slot) AS num_usage, COALESCE(ROUND(SUM(EXTRACT(EPOCH FROM (s.hop_off - ' \
              's.hop_on))) / 3600),0) AS playtime, COUNT(DISTINCT s.player_ucid) AS players FROM missions m, ' \
              'statistics s WHERE m.id = s.mission_id'
        if server_name:
            sql += ' AND m.server_name = %s'
        if period:
            sql += f" AND DATE(s.hop_on) > (DATE(NOW()) - interval '1 {period}')"
        sql += f" GROUP BY s.slot ORDER BY 3 DESC LIMIT {limit}"

        conn = self.pool.getconn()
        try:
            with closing(conn.cursor(cursor_factory=psycopg2.extras.DictCursor)) as cursor:
                modules = playtimes = players = ''
                cursor.execute(sql, (server_name, ))
                for row in cursor.fetchall():
                    modules += row['slot'] + '\n'
                    playtimes += '{:.0f}\n'.format(row['playtime'])
                    players += '{:.0f} ({:.0f})\n'.format(row['players'], row['num_usage'])
                if len(modules) > 0:
                    self.add_field(name=f"TOP {limit} Modules", value=modules)
                    self.add_field(name='Playtime (h)', value=playtimes)
                    self.add_field(name='Players (# uses)', value=players)
        except (Exception, psycopg2.DatabaseError) as error:
            self.log.exception(error)
        finally:
            self.pool.putconn(conn)


class UniquePast14(report.GraphElement):

    def render(self, server_name: Optional[str]):
        sql = 'SELECT d.date AS date, COUNT(DISTINCT s.player_ucid) AS players FROM statistics s, ' \
              'missions m, generate_series(DATE(NOW()) - INTERVAL \'2 weeks\', DATE(NOW()), INTERVAL \'1 ' \
              'day\') d WHERE d.date BETWEEN DATE(s.hop_on) AND DATE(s.hop_off) AND s.mission_id = m.id'
        if server_name:
            sql += f" AND m.server_name = %s"
        sql += ' GROUP BY d.date'

        labels = []
        values = []
        conn = self.pool.getconn()
        try:
            with closing(conn.cursor(cursor_factory=psycopg2.extras.DictCursor)) as cursor:
                cursor.execute(sql, (server_name, ))
                for row in cursor.fetchall():
                    labels.append(row['date'].strftime('%a %m/%d'))
                    values.append(row['players'])
                self.axes.bar(labels, values, width=0.5, color='dodgerblue')
                self.axes.set_title('Unique Players past 14 Days', color='white', fontsize=25)
                self.axes.set_yticks([])
                for label in self.axes.get_xticklabels():
                    label.set_rotation(30)
                    label.set_ha('right')
                for i in range(0, len(values)):
                    self.axes.annotate(values[i], xy=(
                        labels[i], values[i]), ha='center', va='bottom', weight='bold')
                if len(values) == 0:
                    self.axes.set_xticks([])
                    self.axes.text(0, 0, 'No data available.', ha='center', va='center', rotation=45, size=15)
        except (Exception, psycopg2.DatabaseError) as error:
            self.log.exception(error)
        finally:
            self.pool.putconn(conn)


class UsersPerDayTime(report.GraphElement):

    def render(self, server_name: Optional[str], period: Optional[str]):
        sql = 'SELECT to_char(s.hop_on, \'ID\') as weekday, to_char(h.time, \'HH24\') AS hour, ' \
              'COUNT(DISTINCT s.player_ucid) AS players FROM statistics s, missions m, generate_series(current_date, ' \
              'current_date + 1, INTERVAL \'1 hour\') h WHERE date_part(\'hour\', h.time) BETWEEN date_part(\'hour\', ' \
              's.hop_on) AND date_part(\'hour\', s.hop_off) AND s.mission_id = m.id'
        if server_name:
            sql += f" AND m.server_name = %s"
        if period:
            sql += f" AND DATE(s.hop_on) > (DATE(NOW()) - interval '1 {period}')"
        sql += ' GROUP BY 1, 2'

        conn = self.pool.getconn()
        try:
            with closing(conn.cursor(cursor_factory=psycopg2.extras.DictCursor)) as cursor:
                values = np.zeros((24, 7))
                cursor.execute(sql, (server_name, ))
                for row in cursor.fetchall():
                    values[int(row['hour'])][int(row['weekday']) - 1] = row['players']
                self.axes.imshow(values, cmap='cividis', aspect='auto')
                self.axes.set_title('Users per Day/Time (UTC)', color='white', fontsize=25)
                self.axes.xaxis.set_major_formatter(FuncFormatter(lambda x, pos: const.WEEKDAYS[int(np.clip(x, 0, 6))]))
        except (Exception, psycopg2.DatabaseError) as error:
            self.log.exception(error)
        finally:
            self.pool.putconn(conn)


class ServerLoad(report.MultiGraphElement):

    def render(self, server_name: Optional[str], period: str, agent_host: Optional[str]):
        sql = f"SELECT date_trunc('minute', time) AS time, AVG(users) AS \"Users\", AVG(cpu) AS \"CPU\", AVG(CASE " \
              f"WHEN mem_total-mem_ram < 0 THEN 0 ELSE mem_total-mem_ram END)/(1024*1024) AS \"Memory (paged)\", " \
              f"AVG(mem_ram)/(1024*1024) AS \"Memory (RAM)\", SUM(read_bytes)/1024 AS \"Read\", SUM(write_bytes)/1024 " \
              f"AS \"Write\", ROUND(AVG(bytes_sent)) AS \"Sent\", ROUND(AVG(bytes_recv)) AS \"Recv\", ROUND(AVG(fps), " \
              f"2) AS \"FPS\", ROUND(AVG(ping), 2) AS \"Ping\" FROM serverstats " \
              f"WHERE time > (CURRENT_TIMESTAMP - interval '1 {period}')"
        if server_name:
            sql += f" AND server_name = %s"
        if agent_host:
            sql += f" AND agent_host = '{agent_host}'"
        sql += " GROUP BY 1"
        conn = self.pool.getconn()
        try:
            with closing(conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)) as cursor:
                cursor.execute(sql, (server_name, ))
                if cursor.rowcount > 0:
                    series = pd.DataFrame.from_dict(cursor.fetchall())
                    series.plot(ax=self.axes[0], x='time', y=['CPU'], title='CPU / User', xticks=[], xlabel='')
                    self.axes[0].legend(loc='upper left')
                    ax2 = self.axes[0].twinx()
                    series.plot(ax=ax2, x='time', y=['Users'], xticks=[], xlabel='', color='blue')
                    ax2.legend(['Users'], loc='upper right')
                    series.plot(ax=self.axes[1], x='time', y=['FPS'], title='FPS / User', xticks=[], xlabel='')
                    self.axes[1].legend(loc='upper left')
                    ax3 = self.axes[1].twinx()
                    series.plot(ax=ax3, x='time', y=['Users'], xticks=[], xlabel='', color='blue')
                    ax3.legend(['Users'], loc='upper right')
                    series.plot(ax=self.axes[2], x='time', y=['Memory (RAM)', 'Memory (paged)'], title='Memory', xticks=[], xlabel="", ylabel='Memory (MB)', kind='area', stacked=True)
                    self.axes[2].legend(loc='upper left')
                    series.plot(ax=self.axes[3], x='time', y=['Read', 'Write'], title='Disk', logy=True, xticks=[], xlabel='', ylabel='KB', grid=True)
                    self.axes[3].legend(loc='upper left')
                    series.plot(ax=self.axes[4], x='time', y=['Sent', 'Recv'], title='Network', logy=True, xlabel='', ylabel='KB/s', grid=True)
                    self.axes[4].legend(['Sent', 'Recv'], loc='upper left')
                    ax4 = self.axes[4].twinx()
                    series.plot(ax=ax4, x='time', y=['Ping'], xlabel='', ylabel='ms', color='yellow')
                    ax4.legend(['Ping'], loc='upper right')
                else:
                    for i in range(0, 4):
                        self.axes[i].bar([], [])
                        self.axes[i].set_xticks([])
                        self.axes[i].set_yticks([])
                        self.axes[i].text(0, 0, 'No data available.', ha='center', va='center', size=20)
        except (Exception, psycopg2.DatabaseError) as error:
            self.log.exception(error)
        finally:
            self.pool.putconn(conn)
