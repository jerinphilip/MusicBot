import sys
import time
import asyncio
import aiohttp
import discord

from collections import defaultdict

from . import exceptions
from . import downloader

from .config import Config, ConfigDefaults
from .permissions import Permissions, PermissionsDefaults
from .aliases import Aliases, AliasesDefault
from .utils import load_file
from .spotify import Spotify
from .json import Json

from .constants import VERSION as BOTVERSION

from .bot import MusicBot
from .bot import log, intents


class MusicBotAdapter(MusicBot):
    def __init__(self, config_file=None, perms_file=None, aliases_file=None):
        try:
            sys.stdout.write("\x1b]2;MusicBot {}\x07".format(BOTVERSION))
        except:
            pass

        print()

        if config_file is None:
            config_file = ConfigDefaults.options_file

        if perms_file is None:
            perms_file = PermissionsDefaults.perms_file

        if aliases_file is None:
            aliases_file = AliasesDefault.aliases_file

        self.players = {}
        self.exit_signal = None
        self.init_ok = False
        self.cached_app_info = None
        self.last_status = None

        self.config = Config(config_file)

        self._setup_logging()

        self.permissions = Permissions(perms_file, grant_all=[self.config.owner_id])
        self.str = Json(self.config.i18n_file)

        if self.config.usealias:
            self.aliases = Aliases(aliases_file)

        self.blacklist = set(load_file(self.config.blacklist_file))
        self.autoplaylist = load_file(self.config.auto_playlist_file)

        self.aiolocks = defaultdict(asyncio.Lock)
        self.downloader = downloader.Downloader(download_folder="audio_cache")

        log.info("Starting MusicBot {}".format(BOTVERSION))

        if not self.autoplaylist:
            log.warning("Autoplaylist is empty, disabling.")
            self.config.auto_playlist = False
        else:
            log.info("Loaded autoplaylist with {} entries".format(len(self.autoplaylist)))

        if self.blacklist:
            log.debug("Loaded blacklist with {} entries".format(len(self.blacklist)))

        # TODO: Do these properly
        ssd_defaults = {"last_np_msg": None, "auto_paused": False, "availability_paused": False}
        self.server_specific_data = defaultdict(ssd_defaults.copy)

        discord.Client.__init__(self, intents=intents)
        self.aiosession = aiohttp.ClientSession(loop=self.loop)
        self.http.user_agent += " MusicBot/%s" % BOTVERSION

        self.setup_spotify()

    def setup_spotify(self):
        self.spotify = None
        if self.config._spotify:
            try:
                self.spotify = Spotify(
                    self.config.spotify_clientid,
                    self.config.spotify_clientsecret,
                    aiosession=self.aiosession,
                    loop=self.loop,
                )
                if not self.spotify.token:
                    log.warning("Spotify did not provide us with a token. Disabling.")
                    self.config._spotify = False
                else:
                    log.info("Authenticated with Spotify successfully using client ID and secret.")
            except exceptions.SpotifyError as e:
                log.warning(
                    "There was a problem initialising the connection to Spotify. Is your client ID and secret correct? Details: {0}. Continuing anyway in 5 seconds...".format(
                        e
                    )
                )
                self.config._spotify = False
                time.sleep(5)  # make sure they see the problem
        else:
            try:
                log.warning("The config did not have Spotify app credentials, attempting to use guest mode.")
                self.spotify = Spotify(None, None, aiosession=self.aiosession, loop=self.loop)
                if not self.spotify.token:
                    log.warning("Spotify did not provide us with a token. Disabling.")
                    self.config._spotify = False
                else:
                    log.info("Authenticated with Spotify successfully using guest mode.")
                    self.config._spotify = True
            except exceptions.SpotifyError as e:
                log.warning(
                    "There was a problem initialising the connection to Spotify using guest mode. Details: {0}.".format(
                        e
                    )
                )
                self.config._spotify = False

    # TODO(jerinphilip)
    # noinspection PyMethodOverriding
    def run(self, *args, **kwargs):
        try:
            self.loop.run_until_complete(self.start(*args, **kwargs))

        except discord.errors.LoginFailure:
            # Add if token, else
            raise exceptions.HelpfulError(
                "Bot cannot login, bad credentials.",
                "Fix your token in the options file.  " "Remember that each field should be on their own line.",
            )  #     ^^^^ In theory self.config.auth should never have no items

        finally:
            try:
                self._cleanup()
            except Exception:
                log.error("Error in cleanup", exc_info=True)

            if self.exit_signal:
                raise self.exit_signal  # pylint: disable=E0702

    # async def logout(self):
    # async def on_error(self, event, *args, **kwargs):
    # async def on_resumed(self):
    # async def on_ready(self):
    # async def on_message(self, message):
    # async def on_voice_state_update(self, member, before, after):
    # async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
    # async def on_guild_join(self, guild: discord.Guild):
    # async def on_guild_remove(self, guild: discord.Guild):
    # async def on_guild_available(self, guild: discord.Guild):
    # async def on_guild_unavailable(self, guild: discord.Guild):

