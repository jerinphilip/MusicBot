import os
import sys
import time
import shlex
import shutil
import random
import inspect
import logging
import asyncio
import pathlib
import traceback
import math
import re

import aiohttp
import random
import inspect
import logging
import asyncio
import pathlib
import traceback
import math
import re

import aiohttp
import discord
import colorlog

from io import BytesIO, StringIO
from functools import wraps
from textwrap import dedent
from datetime import timedelta
from collections import defaultdict

from discord.enums import ChannelType

from . import exceptions
from . import downloader

from .playlist import Playlist
from .player import MusicPlayer
from .entry import StreamPlaylistEntry
from .opus_loader import load_opus_lib
from .config import Config, ConfigDefaults
from .permissions import Permissions, PermissionsDefaults
from .aliases import Aliases, AliasesDefault
from .constructs import SkipState, Response
from .utils import load_file, write_file, fixg, ftimedelta, _func_, _get_variable
from .spotify import Spotify
from .json import Json

from .constants import VERSION as BOTVERSION
from .constants import DISCORD_MSG_CHAR_LIMIT, AUDIO_CACHE_PATH

load_opus_lib()
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
    def run(self):
        try:
            self.loop.run_until_complete(self.start(*self.config.auth))

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

    # TODO
    async def logout(self):
        await self.disconnect_all_voice_clients()
        return await super().logout()

    # TODO(jerinphilip): mark
    async def on_error(self, event, *args, **kwargs):
        ex_type, ex, stack = sys.exc_info()

        if ex_type == exceptions.HelpfulError:
            log.error("Exception in {}:\n{}".format(event, ex.message))

            await asyncio.sleep(2)  # don't ask
            await self.logout()

        elif issubclass(ex_type, exceptions.Signal):
            self.exit_signal = ex_type
            await self.logout()

        else:
            log.error("Exception in {}".format(event), exc_info=True)

    # TODO(jerinphilip): mark
    async def on_resumed(self):
        log.info("\nReconnected to discord.\n")

    # TODO(jerinphilip): mark
    async def on_ready(self):
        dlogger = logging.getLogger("discord")
        for h in dlogger.handlers:
            if getattr(h, "terminator", None) == "":
                dlogger.removeHandler(h)
                print()

        log.debug("Connection established, ready to go.")

        self.ws._keep_alive.name = "Gateway Keepalive"

        if self.init_ok:
            log.debug("Received additional READY event, may have failed to resume")
            return

        await self._on_ready_sanity_checks()

        self.init_ok = True

        ################################

        log.info("Connected: {0}/{1}#{2}".format(self.user.id, self.user.name, self.user.discriminator))

        owner = self._get_owner(voice=True) or self._get_owner()
        if owner and self.guilds:
            log.info("Owner:     {0}/{1}#{2}\n".format(owner.id, owner.name, owner.discriminator))

            log.info("Guild List:")
            unavailable_servers = 0
            for s in self.guilds:
                ser = "{} (unavailable)".format(s.name) if s.unavailable else s.name
                log.info(" - " + ser)
                if self.config.leavenonowners:
                    if s.unavailable:
                        unavailable_servers += 1
                    else:
                        check = s.get_member(owner.id)
                        if check == None:
                            await s.leave()
                            log.info("Left {} due to bot owner not found".format(s.name))
            if unavailable_servers != 0:
                log.info(
                    "Not proceeding with checks in {} servers due to unavailability".format(str(unavailable_servers))
                )

        elif self.guilds:
            log.warning("Owner could not be found on any guild (id: %s)\n" % self.config.owner_id)

            log.info("Guild List:")
            for s in self.guilds:
                ser = "{} (unavailable)".format(s.name) if s.unavailable else s.name
                log.info(" - " + ser)

        else:
            log.warning("Owner unknown, bot is not on any guilds.")
            if self.user.bot:
                log.warning(
                    "To make the bot join a guild, paste this link in your browser. \n"
                    "Note: You should be logged into your main account and have \n"
                    "manage server permissions on the guild you want the bot to join.\n"
                    "  " + await self.generate_invite_link()
                )

        print(flush=True)

        if self.config.bound_channels:
            chlist = set(self.get_channel(i) for i in self.config.bound_channels if i)
            chlist.discard(None)

            invalids = set()
            invalids.update(c for c in chlist if isinstance(c, discord.VoiceChannel))

            chlist.difference_update(invalids)
            self.config.bound_channels.difference_update(invalids)

            if chlist:
                log.info("Bound to text channels:")
                [log.info(" - {}/{}".format(ch.guild.name.strip(), ch.name.strip())) for ch in chlist if ch]
            else:
                print("Not bound to any text channels")

            if invalids and self.config.debug_mode:
                print(flush=True)
                log.info("Not binding to voice channels:")
                [log.info(" - {}/{}".format(ch.guild.name.strip(), ch.name.strip())) for ch in invalids if ch]

            print(flush=True)

        else:
            log.info("Not bound to any text channels")

        if self.config.autojoin_channels:
            chlist = set(self.get_channel(i) for i in self.config.autojoin_channels if i)
            chlist.discard(None)

            invalids = set()
            invalids.update(c for c in chlist if isinstance(c, discord.TextChannel))

            chlist.difference_update(invalids)
            self.config.autojoin_channels.difference_update(invalids)

            if chlist:
                log.info("Autojoining voice channels:")
                [log.info(" - {}/{}".format(ch.guild.name.strip(), ch.name.strip())) for ch in chlist if ch]
            else:
                log.info("Not autojoining any voice channels")

            if invalids and self.config.debug_mode:
                print(flush=True)
                log.info("Cannot autojoin text channels:")
                [log.info(" - {}/{}".format(ch.guild.name.strip(), ch.name.strip())) for ch in invalids if ch]

            self.autojoin_channels = chlist

        else:
            log.info("Not autojoining any voice channels")
            self.autojoin_channels = set()

        if self.config.show_config_at_start:
            print(flush=True)
            log.info("Options:")

            log.info("  Command prefix: " + self.config.command_prefix)
            log.info("  Default volume: {}%".format(int(self.config.default_volume * 100)))
            log.info(
                "  Skip threshold: {} votes or {}%".format(
                    self.config.skips_required, fixg(self.config.skip_ratio_required * 100)
                )
            )
            log.info("  Now Playing @mentions: " + ["Disabled", "Enabled"][self.config.now_playing_mentions])
            log.info("  Auto-Summon: " + ["Disabled", "Enabled"][self.config.auto_summon])
            log.info(
                "  Auto-Playlist: "
                + ["Disabled", "Enabled"][self.config.auto_playlist]
                + " (order: "
                + ["sequential", "random"][self.config.auto_playlist_random]
                + ")"
            )
            log.info("  Auto-Pause: " + ["Disabled", "Enabled"][self.config.auto_pause])
            log.info("  Delete Messages: " + ["Disabled", "Enabled"][self.config.delete_messages])
            if self.config.delete_messages:
                log.info("    Delete Invoking: " + ["Disabled", "Enabled"][self.config.delete_invoking])
            log.info("  Debug Mode: " + ["Disabled", "Enabled"][self.config.debug_mode])
            log.info("  Downloaded songs will be " + ["deleted", "saved"][self.config.save_videos])
            if self.config.status_message:
                log.info("  Status message: " + self.config.status_message)
            log.info("  Write current songs to file: " + ["Disabled", "Enabled"][self.config.write_current_song])
            log.info("  Author insta-skip: " + ["Disabled", "Enabled"][self.config.allow_author_skip])
            log.info("  Embeds: " + ["Disabled", "Enabled"][self.config.embeds])
            log.info("  Spotify integration: " + ["Disabled", "Enabled"][self.config._spotify])
            log.info("  Legacy skip: " + ["Disabled", "Enabled"][self.config.legacy_skip])
            log.info("  Leave non owners: " + ["Disabled", "Enabled"][self.config.leavenonowners])

        print(flush=True)

        await self.update_now_playing_status()

        # maybe option to leave the ownerid blank and generate a random command for the owner to use
        # wait_for_message is pretty neato

        await self._join_startup_channels(self.autojoin_channels, autosummon=self.config.auto_summon)

        # we do this after the config stuff because it's a lot easier to notice here
        if self.config.missing_keys:
            log.warning(
                "Your config file is missing some options. If you have recently updated, "
                "check the example_options.ini file to see if there are new options available to you. "
                "The options missing are: {0}".format(self.config.missing_keys)
            )
            print(flush=True)

        # t-t-th-th-that's all folks!

    # TODO(jerinphilip): mark
    async def on_message(self, message):
        await self.wait_until_ready()

        message_content = message.content.strip()
        if not message_content.startswith(self.config.command_prefix):
            return

        if message.author == self.user:
            log.warning("Ignoring command from myself ({})".format(message.content))
            return

        if message.author.bot and message.author.id not in self.config.bot_exception_ids:
            log.warning("Ignoring command from other bot ({})".format(message.content))
            return

        if (not isinstance(message.channel, discord.abc.GuildChannel)) and (
            not isinstance(message.channel, discord.abc.PrivateChannel)
        ):
            return

        command, *args = message_content.split(
            " "
        )  # Uh, doesn't this break prefixes with spaces in them (it doesn't, config parser already breaks them)
        command = command[len(self.config.command_prefix) :].lower().strip()

        # [] produce [''] which is not what we want (it break things)
        if args:
            args = " ".join(args).lstrip(" ").split(" ")
        else:
            args = []

        handler = getattr(self, "cmd_" + command, None)
        if not handler:
            # alias handler
            if self.config.usealias:
                command = self.aliases.get(command)
                handler = getattr(self, "cmd_" + command, None)
                if not handler:
                    return
            else:
                return

        if isinstance(message.channel, discord.abc.PrivateChannel):
            if not (message.author.id == self.config.owner_id and command == "joinserver"):
                await self.safe_send_message(message.channel, "You cannot use this bot in private messages.")
                return

        if self.config.bound_channels and message.channel.id not in self.config.bound_channels:
            if self.config.unbound_servers:
                for channel in message.guild.channels:
                    if channel.id in self.config.bound_channels:
                        return
            else:
                return  # if I want to log this I just move it under the prefix check

        if message.author.id in self.blacklist and message.author.id != self.config.owner_id:
            log.warning("User blacklisted: {0.id}/{0!s} ({1})".format(message.author, command))
            return

        else:
            log.info("{0.id}/{0!s}: {1}".format(message.author, message_content.replace("\n", "\n... ")))

        user_permissions = self.permissions.for_user(message.author)

        argspec = inspect.signature(handler)
        params = argspec.parameters.copy()

        sentmsg = response = None

        # noinspection PyBroadException
        try:
            if user_permissions.ignore_non_voice and command in user_permissions.ignore_non_voice:
                await self._check_ignore_non_voice(message)

            handler_kwargs = {}
            if params.pop("message", None):
                handler_kwargs["message"] = message

            if params.pop("channel", None):
                handler_kwargs["channel"] = message.channel

            if params.pop("author", None):
                handler_kwargs["author"] = message.author

            if params.pop("guild", None):
                handler_kwargs["guild"] = message.guild

            if params.pop("player", None):
                handler_kwargs["player"] = await self.get_player(message.channel)

            if params.pop("_player", None):
                handler_kwargs["_player"] = self.get_player_in(message.guild)

            if params.pop("permissions", None):
                handler_kwargs["permissions"] = user_permissions

            if params.pop("user_mentions", None):
                handler_kwargs["user_mentions"] = list(map(message.guild.get_member, message.raw_mentions))

            if params.pop("channel_mentions", None):
                handler_kwargs["channel_mentions"] = list(map(message.guild.get_channel, message.raw_channel_mentions))

            if params.pop("voice_channel", None):
                handler_kwargs["voice_channel"] = message.guild.me.voice.channel if message.guild.me.voice else None

            if params.pop("leftover_args", None):
                handler_kwargs["leftover_args"] = args

            args_expected = []
            for key, param in list(params.items()):

                # parse (*args) as a list of args
                if param.kind == param.VAR_POSITIONAL:
                    handler_kwargs[key] = args
                    params.pop(key)
                    continue

                # parse (*, args) as args rejoined as a string
                # multiple of these arguments will have the same value
                if param.kind == param.KEYWORD_ONLY and param.default == param.empty:
                    handler_kwargs[key] = " ".join(args)
                    params.pop(key)
                    continue

                doc_key = "[{}={}]".format(key, param.default) if param.default is not param.empty else key
                args_expected.append(doc_key)

                # Ignore keyword args with default values when the command had no arguments
                if not args and param.default is not param.empty:
                    params.pop(key)
                    continue

                # Assign given values to positional arguments
                if args:
                    arg_value = args.pop(0)
                    handler_kwargs[key] = arg_value
                    params.pop(key)

            if message.author.id != self.config.owner_id:
                if user_permissions.command_whitelist and command not in user_permissions.command_whitelist:
                    raise exceptions.PermissionsError(
                        "This command is not enabled for your group ({}).".format(user_permissions.name), expire_in=20
                    )

                elif user_permissions.command_blacklist and command in user_permissions.command_blacklist:
                    raise exceptions.PermissionsError(
                        "This command is disabled for your group ({}).".format(user_permissions.name), expire_in=20
                    )

            # Invalid usage, return docstring
            if params:
                docs = getattr(handler, "__doc__", None)
                if not docs:
                    docs = "Usage: {}{} {}".format(self.config.command_prefix, command, " ".join(args_expected))

                docs = dedent(docs)
                await self.safe_send_message(
                    message.channel,
                    "```\n{}\n```".format(docs.format(command_prefix=self.config.command_prefix)),
                    expire_in=60,
                )
                return

            response = await handler(**handler_kwargs)
            if response and isinstance(response, Response):
                if not isinstance(response.content, discord.Embed) and self.config.embeds:
                    content = self._gen_embed()
                    content.title = command
                    content.description = response.content
                else:
                    content = response.content

                if response.reply:
                    if isinstance(content, discord.Embed):
                        content.description = "{} {}".format(
                            message.author.mention,
                            content.description if content.description is not discord.Embed.Empty else "",
                        )
                    else:
                        content = "{}: {}".format(message.author.mention, content)

                sentmsg = await self.safe_send_message(
                    message.channel,
                    content,
                    expire_in=response.delete_after if self.config.delete_messages else 0,
                    also_delete=message if self.config.delete_invoking else None,
                )

        except (exceptions.CommandError, exceptions.HelpfulError, exceptions.ExtractionError) as e:
            log.error("Error in {0}: {1.__class__.__name__}: {1.message}".format(command, e), exc_info=True)

            expirein = e.expire_in if self.config.delete_messages else None
            alsodelete = message if self.config.delete_invoking else None

            if self.config.embeds:
                content = self._gen_embed()
                content.add_field(name="Error", value=e.message, inline=False)
                content.colour = 13369344
            else:
                content = "```\n{}\n```".format(e.message)

            await self.safe_send_message(message.channel, content, expire_in=expirein, also_delete=alsodelete)

        except exceptions.Signal:
            raise

        except Exception:
            log.error("Exception in on_message", exc_info=True)
            if self.config.debug_mode:
                await self.safe_send_message(message.channel, "```\n{}\n```".format(traceback.format_exc()))

        finally:
            if not sentmsg and not response and self.config.delete_invoking:
                await asyncio.sleep(5)
                await self.safe_delete_message(message, quiet=True)

    async def gen_cmd_list(self, message, list_all_cmds=False):
        for att in dir(self):
            # This will always return at least cmd_help, since they needed perms to run this command
            if att.startswith("cmd_") and not hasattr(getattr(self, att), "dev_cmd"):
                user_permissions = self.permissions.for_user(message.author)
                command_name = att.replace("cmd_", "").lower()
                whitelist = user_permissions.command_whitelist
                blacklist = user_permissions.command_blacklist
                if list_all_cmds:
                    self.commands.append("{}{}".format(self.config.command_prefix, command_name))

                elif blacklist and command_name in blacklist:
                    pass

                elif whitelist and command_name not in whitelist:
                    pass

                else:
                    self.commands.append("{}{}".format(self.config.command_prefix, command_name))

    # TODO(jerinphilip): mark
    async def on_voice_state_update(self, member, before, after):
        if not self.init_ok:
            return  # Ignore stuff before ready

        if before.channel:
            channel = before.channel
        elif after.channel:
            channel = after.channel
        else:
            return

        if member == self.user and not after.channel:  # if bot was disconnected from channel
            await self.disconnect_voice_client(before.channel.guild)
            return

        if not self.config.auto_pause:
            return

        autopause_msg = "{state} in {channel.guild.name}/{channel.name} {reason}"

        auto_paused = self.server_specific_data[channel.guild]["auto_paused"]

        try:
            player = await self.get_player(channel)
        except exceptions.CommandError:
            return

        def is_active(member):
            if not member.voice:
                return False

            if any([member.voice.deaf, member.voice.self_deaf, member.bot]):
                return False

            return True

        if not member == self.user and is_active(member):  # if the user is not inactive
            if (
                player.voice_client.channel != before.channel and player.voice_client.channel == after.channel
            ):  # if the person joined
                if auto_paused and player.is_paused:
                    log.info(
                        autopause_msg.format(state="Unpausing", channel=player.voice_client.channel, reason="").strip()
                    )

                    self.server_specific_data[player.voice_client.guild]["auto_paused"] = False
                    player.resume()
            elif player.voice_client.channel == before.channel and player.voice_client.channel != after.channel:
                if not any(is_active(m) for m in player.voice_client.channel.members):  # channel is empty
                    if not auto_paused and player.is_playing:
                        log.info(
                            autopause_msg.format(
                                state="Pausing", channel=player.voice_client.channel, reason="(empty channel)"
                            ).strip()
                        )

                        self.server_specific_data[player.voice_client.guild]["auto_paused"] = True
                        player.pause()
            elif (
                player.voice_client.channel == before.channel and player.voice_client.channel == after.channel
            ):  # if the person undeafen
                if auto_paused and player.is_paused:
                    log.info(
                        autopause_msg.format(
                            state="Unpausing", channel=player.voice_client.channel, reason="(member undeafen)"
                        ).strip()
                    )

                    self.server_specific_data[player.voice_client.guild]["auto_paused"] = False
                    player.resume()
        else:
            if any(is_active(m) for m in player.voice_client.channel.members):  # channel is not empty
                if auto_paused and player.is_paused:
                    log.info(
                        autopause_msg.format(state="Unpausing", channel=player.voice_client.channel, reason="").strip()
                    )

                    self.server_specific_data[player.voice_client.guild]["auto_paused"] = False
                    player.resume()

            else:
                if not auto_paused and player.is_playing:
                    log.info(
                        autopause_msg.format(
                            state="Pausing",
                            channel=player.voice_client.channel,
                            reason="(empty channel or member deafened)",
                        ).strip()
                    )

                    self.server_specific_data[player.voice_client.guild]["auto_paused"] = True
                    player.pause()

    # TODO(jerinphilip): mark
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        if before.region != after.region:
            log.warning('Guild "%s" changed regions: %s -> %s' % (after.name, before.region, after.region))

    # TODO(jerinphilip): mark
    async def on_guild_join(self, guild: discord.Guild):
        log.info("Bot has been added to guild: {}".format(guild.name))
        owner = self._get_owner(voice=True) or self._get_owner()
        if self.config.leavenonowners:
            check = guild.get_member(owner.id)
            if check == None:
                await guild.leave()
                log.info("Left {} due to bot owner not found.".format(guild.name))
                await owner.send(
                    self.str.get(
                        "left-no-owner-guilds", "Left `{}` due to bot owner not being found in it.".format(guild.name)
                    )
                )

        log.debug("Creating data folder for guild %s", guild.id)
        pathlib.Path("data/%s/" % guild.id).mkdir(exist_ok=True)

    # TODO(jerinphilip): mark
    async def on_guild_remove(self, guild: discord.Guild):
        log.info("Bot has been removed from guild: {}".format(guild.name))
        log.debug("Updated guild list:")
        [log.debug(" - " + s.name) for s in self.guilds]

        if guild.id in self.players:
            self.players.pop(guild.id).kill()

    # TODO(jerinphilip): mark
    async def on_guild_available(self, guild: discord.Guild):
        if not self.init_ok:
            return  # Ignore pre-ready events

        log.debug('Guild "{}" has become available.'.format(guild.name))

        player = self.get_player_in(guild)

        if player and player.is_paused:
            av_paused = self.server_specific_data[guild]["availability_paused"]

            if av_paused:
                log.debug('Resuming player in "{}" due to availability.'.format(guild.name))
                self.server_specific_data[guild]["availability_paused"] = False
                player.resume()

    # TODO(jerinphilip): mark
    async def on_guild_unavailable(self, guild: discord.Guild):
        log.debug('Guild "{}" has become unavailable.'.format(guild.name))

        player = self.get_player_in(guild)

        if player and player.is_playing:
            log.debug('Pausing player in "{}" due to unavailability.'.format(guild.name))
            self.server_specific_data[guild]["availability_paused"] = True
            player.pause()

    def voice_client_in(self, guild):
        for vc in self.voice_clients:
            if vc.guild == guild:
                return vc
        return None
