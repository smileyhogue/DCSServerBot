import aiohttp
import asyncio
import discord
import os
import psycopg
import re
import shutil
import zipfile

from contextlib import closing, suppress
from core import Status, Plugin, PluginConfigurationError, utils, Server, PluginInstallationError, command, DEFAULT_TAG
from discord import SelectOption, TextStyle, app_commands
from discord.ui import View, Select, Button, Modal, TextInput
from filecmp import cmp
from psycopg.rows import dict_row
from services import DCSServerBot
from typing import Optional, Tuple
from urllib.parse import urlparse, unquote

OVGME_FOLDERS = ['RootFolder', 'SavedGames']


class OvGME(Plugin):

    async def install(self) -> None:
        await super().install()
        if not self.locals:
            raise PluginInstallationError(reason=f"No {self.plugin_name}.yaml file found!", plugin=self.plugin_name)
        config = self.get_config()
        if not config:
            raise PluginConfigurationError(plugin=self.plugin_name, option=DEFAULT_TAG)
        for folder in OVGME_FOLDERS:
            if folder not in config:
                raise PluginConfigurationError(self.plugin_name, folder)
        asyncio.create_task(self.install_packages())

    async def before_dcs_update(self):
        # uninstall all RootFolder-packages
        for server_name, server in self.bot.servers.items():
            for package_name, version in self.get_installed_packages(server, 'RootFolder'):
                await self.uninstall_package(server, 'RootFolder', package_name, version)

    async def after_dcs_update(self):
        await self.install_packages()

    def rename(self, conn: psycopg.Connection, old_name: str, new_name: str):
        conn.execute('UPDATE ovgme_packages SET server_name = %s WHERE server_name = %s', (new_name, old_name))

    @staticmethod
    def parse_filename(filename: str) -> Tuple[Optional[str], Optional[str]]:
        if filename.endswith('.zip'):
            filename = filename[:-4]
        exp = re.compile('(?P<package>.*)_v(?P<version>.*)')
        match = exp.match(filename)
        if match:
            return match.group('package'), match.group('version')
        else:
            return None, None

    @staticmethod
    def is_greater(v1: str, v2: str):
        parts1 = [int(x) for x in v1.split('.')]
        parts2 = [int(x) for x in v2.split('.')]
        for i in range(0, max(len(parts1), len(parts2))):
            if parts1[i] > parts2[i]:
                return True
        return False

    async def install_packages(self):
        if not self.locals or 'configs' not in self.locals:
            return
        for server_name, server in self.bot.servers.items():
            # wait for the servers to be registered
            while server.status == Status.UNREGISTERED:
                await asyncio.sleep(1)
            config = self.get_config(server)
            if 'packages' not in config:
                return

            for package in config['packages']:
                version = package['version'] if package['version'] != 'latest' \
                    else self.get_latest_version(package['source'], package['name'])
                installed = self.check_package(server, package['source'], package['name'])
                if (not installed or installed != version) and \
                        server.status != Status.SHUTDOWN:
                    self.log.warning(f"  - Server {server.name} needs to be shutdown to install packages.")
                    break
                maintenance = server.maintenance
                server.maintenance = True
                try:
                    if not installed:
                        if await self.install_package(server, package['source'], package['name'], version):
                            self.log.info(f"- Package {package['name']}_v{version} installed.")
                        else:
                            self.log.warning(f"- Package {package['name']}_v{version} not found!")
                    elif installed != version:
                        if self.is_greater(installed, version):
                            self.log.debug(f"- Installed package {package['name']}_v{installed} is newer than the "
                                           f"configured version. Skipping.")
                            continue
                        if not await self.uninstall_package(server, package['source'], package['name'], installed):
                            self.log.warning(f"- Package {package['name']}_v{installed} could not be uninstalled!")
                        elif not await self.install_package(server, package['source'], package['name'], version):
                            self.log.warning(f"- Package {package['name']}_v{version} could not be installed!")
                        else:
                            self.log.info(f"- Package {package['name']}_v{installed} updated to v{version}.")
                finally:
                    if maintenance:
                        server.maintenance = maintenance
                    else:
                        server.maintenance = False

    def get_latest_version(self, folder: str, package: str) -> str:
        config = self.get_config()
        path = os.path.expandvars(config[folder])
        available = [self.parse_filename(x) for x in os.listdir(path) if package in x]
        max_version = None
        for _, version in available:
            if not max_version or self.is_greater(version, max_version):
                max_version = version
        return max_version

    def check_package(self, server: Server, folder: str, package_name: str) -> Optional[str]:
        with self.pool.connection() as conn:
            with closing(conn.cursor()) as cursor:
                cursor.execute(
                    'SELECT version FROM ovgme_packages WHERE server_name = %s AND package_name = %s AND folder = %s',
                    (server.name, package_name, folder))
                return cursor.fetchone()[0] if cursor.rowcount == 1 else None

    async def install_package(self, server: Server, folder: str, package_name: str, version: str) -> bool:
        config = self.get_config(server)
        path = os.path.expandvars(config[folder])
        os.makedirs(os.path.join(path, '.' + server.instance.name), exist_ok=True)
        target = os.path.expandvars(self.bot.node.locals['DCS']['installation']) if folder == 'RootFolder' else \
            server.instance.home
        for file in os.listdir(path):
            filename = os.path.join(path, file)
            if (os.path.isfile(filename) and file == package_name + '_v' + version + '.zip') or \
                    (os.path.isdir(filename) and file == package_name + '_v' + version):
                ovgme_path = os.path.join(path, '.' + server.instance.name, package_name + '_v' + version)
                os.makedirs(ovgme_path, exist_ok=True)
                if os.path.isfile(filename) and file == package_name + '_v' + version + '.zip':
                    with open(os.path.join(ovgme_path, 'install.log'), 'w') as log:
                        with zipfile.ZipFile(filename, 'r') as zfile:
                            for name in zfile.namelist():
                                orig = os.path.join(target, name)
                                if os.path.exists(orig) and os.path.isfile(orig):
                                    log.write(f"x {name}\n")
                                    shutil.copy2(orig, os.path.join(ovgme_path, name))
                                else:
                                    log.write(f"w {name}\n")
                                zfile.extract(name, target)
                else:
                    with open(os.path.join(ovgme_path, 'install.log'), 'w') as log:
                        def backup(p, names) -> list[str]:
                            _dir = p[len(os.path.join(path, package_name + '_v' + version)):].lstrip(os.path.sep)
                            for name in names:
                                source = os.path.join(p, name)
                                if len(_dir):
                                    name = os.path.join(_dir, name)
                                orig = os.path.join(target, name)
                                if os.path.exists(orig) and os.path.isfile(orig) and not cmp(source, orig):
                                    log.write("x {}\n".format(name.replace('\\', '/')))
                                    dest = os.path.join(ovgme_path, name)
                                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                                    shutil.copy2(orig, dest)
                                else:
                                    log.write("w {}\n".format(name.replace('\\', '/')))
                            return []

                        shutil.copytree(filename, target, ignore=backup, dirs_exist_ok=True)
                with self.pool.connection() as conn:
                    with conn.transaction():
                        conn.execute("""
                            INSERT INTO ovgme_packages (server_name, package_name, version, folder) 
                            VALUES (%s, %s, %s, %s)
                        """, (server.name, package_name, version, folder))
                return True
        return False

    async def uninstall_package(self, server: Server, folder: str, package_name: str, version: str) -> bool:
        config = self.get_config(server)
        path = os.path.expandvars(config[folder])
        ovgme_path = os.path.join(path, '.' + server.instance.name, package_name + '_v' + version)
        target = os.path.expandvars(self.bot.node.locals['DCS']['installation']) if folder == 'RootFolder' else \
            server.instance.home
        if not os.path.exists(os.path.join(ovgme_path, 'install.log')):
            return False
        with open(os.path.join(ovgme_path, 'install.log')) as log:
            lines = log.readlines()
            # delete has to run reverse to clean the directories
            for i in range(len(lines) - 1, 0, -1):
                filename = lines[i][2:].strip()
                file = os.path.normpath(os.path.join(target, filename))
                if lines[i].startswith('w'):
                    if os.path.isfile(file):
                        os.remove(file)
                    elif os.path.isdir(file):
                        with suppress(Exception):
                            os.removedirs(file)
                elif lines[i].startswith('x'):
                    try:
                        shutil.copy2(os.path.join(ovgme_path, filename), file)
                    except FileNotFoundError:
                        self.log.warning(f"Can't recover file {filename}, because it has been removed! "
                                         f"You might need to run a slow repair.")
        shutil.rmtree(ovgme_path)
        with self.pool.connection() as conn:
            with conn.transaction():
                conn.execute("""
                    DELETE FROM ovgme_packages 
                    WHERE server_name = %s AND folder = %s AND package_name = %s AND version = %s
                """, (server.name, folder, package_name, version))
        return True

    def format_packages(self, data, marker, marker_emoji):
        embed = discord.Embed(title="List of installed Packages", color=discord.Color.blue())
        ids = packages = versions = ''
        flag = False
        for i in range(0, len(data)):
            ids += (chr(0x31 + i) + '\u20E3' + '\n')
            packages += data[i][1] + '\n'
            versions += data[i][2]
            latest = self.get_latest_version(data[i][0], data[i][1])
            if latest != data[i][2]:
                flag = True
                versions += ' ' + marker_emoji + '\n'
            else:
                versions += '\n'
        embed.add_field(name='ID', value=ids)
        embed.add_field(name='Package', value=packages)
        embed.add_field(name='Version', value=versions)
        if flag:
            footer = 'Press a number to update or uninstall.\n' + marker_emoji + ' update available'
        else:
            footer = 'Press a number to uninstall.'
        embed.set_footer(text=footer)
        return embed

    def get_installed_packages(self, server: Server, folder: str) -> list[Tuple[str, str]]:
        with self.pool.connection() as conn:
            with closing(conn.cursor(row_factory=dict_row)) as cursor:
                return [
                    (x['package_name'], x['version']) for x in cursor.execute(
                        """
                        SELECT * FROM ovgme_packages WHERE server_name = %s AND folder = %s
                        """, (server.name, folder)).fetchall()
                ]

    @command(description='Display installed packages')
    @app_commands.guild_only()
    @utils.app_has_roles(['Admin'])
    async def packages(self, interaction: discord.Interaction,
                       server: app_commands.Transform[Server, utils.ServerTransformer(
                           status=[Status.RUNNING, Status.PAUSED, Status.STOPPED, Status.SHUTDOWN])]):
        class PackageView(View):

            def __init__(derived, embed: discord.Embed):
                super().__init__()
                derived.installed = derived.get_installed()
                derived.available = derived.get_available()
                derived.embed = embed
                derived.render()

            def get_installed(derived) -> list[Tuple[str, str, str]]:
                installed = []
                for folder in OVGME_FOLDERS:
                    packages = [(folder, x, y) for x, y in self.get_installed_packages(server, folder)]
                    if packages:
                        installed.extend(packages)
                return installed

            def get_available(derived) -> list[Tuple[str, str, str]]:
                available = []
                config = self.get_config(server)
                for folder in OVGME_FOLDERS:
                    packages = []
                    for x in os.listdir(os.path.expandvars(config[folder])):
                        if x.startswith('.'):
                            continue
                        package, version = self.parse_filename(x)
                        if package:
                            packages.append((folder, package, version))
                        else:
                            self.log.warning(f"{x} could not be parsed!")
                    if packages:
                        available.extend(packages)
                return list(set(available) - set(derived.installed))

            async def shutdown(derived, interaction: discord.Interaction):
                await interaction.response.defer()
                derived.embed.set_footer(text=f"Shutting down {server.name}, please wait ...")
                await interaction.edit_original_response(embed=derived.embed)
                await server.shutdown()
                derived.render()
                await interaction.edit_original_response(embed=derived.embed, view=derived)

            def render(derived):
                derived.embed.clear_fields()
                if derived.installed:
                    derived.embed.add_field(name='_ _', value='**The following mods are currently installed:**',
                                            inline=False)
                    packages = versions = update = ''
                    for i in range(0, len(derived.installed)):
                        packages += derived.installed[i][1] + '\n'
                        versions += derived.installed[i][2] + '\n'
                        latest = self.get_latest_version(derived.installed[i][0], derived.installed[i][1])
                        if latest != derived.installed[i][2]:
                            update += latest + '\n'
                        else:
                            update += '_ _\n'
                    derived.embed.add_field(name='Package', value=packages)
                    derived.embed.add_field(name='Version', value=versions)
                    derived.embed.add_field(name='Update', value=update)
                else:
                    derived.embed.add_field(name='_ _', value='There are no mods installed.', inline=False)

                derived.clear_items()
                if derived.available and server.status == Status.SHUTDOWN:
                    select = Select(placeholder="Select a package to install / update",
                                    options=[SelectOption(label=x[1] + '_' + x[2], value=str(idx))
                                             for idx, x in enumerate(derived.available)],
                                    row=0)
                    select.callback = derived.install
                    derived.add_item(select)
                if derived.installed and server.status == Status.SHUTDOWN:
                    select = Select(placeholder="Select a package to uninstall",
                                    options=[SelectOption(label=x[1] + '_' + x[2], value=str(idx))
                                             for idx, x in enumerate(derived.installed)],
                                    disabled=not derived.installed or server.status != Status.SHUTDOWN,
                                    row=1)
                    select.callback = derived.uninstall
                    derived.add_item(select)
                button = Button(label="Add", style=discord.ButtonStyle.primary, row=2)
                button.callback = derived.add
                derived.add_item(button)
                if server.status != Status.SHUTDOWN:
                    button = Button(label="Shutdown", style=discord.ButtonStyle.secondary, row=2)
                    button.callback = derived.shutdown
                    derived.add_item(button)
                    derived.embed.set_footer(text=f"⚠️ Server {server.name} needs to be shut down to change mods.")
                else:
                    for i in range(1, len(derived.children)):
                        if isinstance(derived.children[i], Button) and derived.children[i].label == "Shutdown":
                            derived.remove_item(derived.children[i])
                button = Button(label="Quit", style=discord.ButtonStyle.red, row=2)
                button.callback = derived.cancel
                derived.add_item(button)

            async def install(derived, interaction: discord.Interaction):
                await interaction.response.defer()
                try:
                    folder, package, version = derived.available[int(interaction.data['values'][0])]
                    current = self.check_package(server, folder, package)
                    if current:
                        derived.embed.set_footer(text=f"Updating package {package}, please wait ...")
                        await interaction.edit_original_response(embed=derived.embed)
                        if not await self.uninstall_package(server, folder, package, current):
                            derived.embed.set_footer(text=f"Package {package}_v{version} could not be uninstalled!")
                            await interaction.edit_original_response(embed=derived.embed)
                        elif not await self.install_package(server, folder, package, version):
                            derived.embed.set_footer(text=f"Package {package}_v{version} could not be installed!")
                            await interaction.edit_original_response(embed=derived.embed)
                        else:
                            derived.embed.set_footer(text=f"Package {package} updated.")
                            derived.installed = derived.get_installed()
                            derived.available = derived.get_available()
                            derived.render()
                    else:
                        derived.embed.set_footer(text=f"Installing package {package}, please wait ...")
                        await interaction.edit_original_response(embed=derived.embed)
                        if not await self.install_package(server, folder, package, version):
                            derived.embed.set_footer(text=f"Installation of package {package} failed.")
                        else:
                            derived.embed.set_footer(text=f"Package {package} installed.")
                            derived.installed = derived.get_installed()
                            derived.available = derived.get_available()
                            derived.render()
                    await interaction.edit_original_response(embed=derived.embed, view=derived)
                except Exception as ex:
                    self.log.exception(ex)

            async def uninstall(derived, interaction: discord.Interaction):
                await interaction.response.defer()
                folder, package, version = derived.installed[int(interaction.data['values'][0])]
                derived.embed.set_footer(text=f"Uninstalling package {package}, please wait ...")
                await interaction.edit_original_response(embed=derived.embed)
                if not await self.uninstall_package(server, folder, package, version):
                    derived.embed.set_footer(text=f"Package {package}_v{version} could not be uninstalled!")
                else:
                    derived.embed.set_footer(text=f"Package {package} uninstalled.")
                    derived.installed = derived.get_installed()
                    derived.available = derived.get_available()
                    derived.render()
                await interaction.edit_original_response(embed=derived.embed, view=derived)

            async def add(derived, interaction: discord.Interaction):
                class UploadModal(Modal, title="Enter the mod URL"):
                    url = TextInput(label="URL", placeholder='https://...', style=TextStyle.short, required=True)
                    filename = TextInput(label="Filename-override (optional)", placeholder="name_vX.Y.Z",
                                         style=TextStyle.short, required=False)
                    dest = TextInput(label="Destination (S=Saved Games / R=Root Folder)", style=TextStyle.short,
                                     required=True, min_length=1, max_length=1)

                    async def on_submit(_, interaction: discord.Interaction) -> None:
                        await interaction.response.defer()

                async def download(modal: UploadModal):
                    async with aiohttp.ClientSession() as session:
                        async with session.get(modal.url.value) as response:
                            if response.status == 200:
                                path = os.path.expandvars(self.get_config(server)[
                                                              OVGME_FOLDERS[0] if modal.dest.value == 'R' else
                                                              OVGME_FOLDERS[1]])
                                if modal.filename.value:
                                    filename = modal.filename.value
                                else:
                                    filename = os.path.basename(unquote(urlparse(modal.url.value).path))
                                self.log.debug(f"Downloading file {filename} from {modal.url.value} ...")
                                with open(os.path.join(path, filename), 'wb') as outfile:
                                    outfile.write(await response.read())
                                self.log.debug(f"File {filename} downloaded.")

                modal = UploadModal()
                await interaction.response.send_modal(modal)
                if not await modal.wait():
                    derived.embed.set_footer(text=f"Downloading {modal.url.value} , please wait ...")
                    for child in derived.children:
                        child.disabled = True
                    await interaction.edit_original_response(embed=derived.embed, view=derived)
                    await download(modal)
                    for child in derived.children:
                        child.disabled = False
                    embed.remove_footer()
                    derived.available = derived.get_available()
                    derived.render()
                    await interaction.edit_original_response(embed=derived.embed, view=derived)

            async def cancel(derived, interaction: discord.Interaction):
                derived.stop()

        embed = discord.Embed(title="Package Manager", color=discord.Color.blue())
        embed.description = f"Install or uninstall mod packages to {server.name}"
        view = PackageView(embed)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        try:
            await view.wait()
        finally:
            await interaction.delete_original_response()


async def setup(bot: DCSServerBot):
    await bot.add_cog(OvGME(bot))
