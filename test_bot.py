import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot import IMPORTANT_GIF_URL, Player, Request, VoiceAudio, bot, match_songs
from controls import PlaybackControls, QueueView
from youtube import MusicError, Track, parse_query


class ChatCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_help_privately_lists_every_command(self):
        interaction = SimpleNamespace(response=AsyncMock())
        await bot.tree.get_command("help").callback(interaction)
        reply = interaction.response.send_message.await_args
        self.assertTrue(reply.kwargs["ephemeral"])
        for command in bot.tree.get_commands():
            self.assertIn(f"`/{command.name}`", reply.args[0])

    async def test_important_posts_in_chat_with_link_fallback(self):
        interaction = SimpleNamespace(response=AsyncMock(), app_permissions=SimpleNamespace(embed_links=True))
        command = bot.tree.get_command("important")
        await command.callback(interaction)
        reply = interaction.response.send_message.await_args
        self.assertFalse(reply.kwargs.get("ephemeral", False))
        self.assertEqual(reply.kwargs["embed"].image.url, IMPORTANT_GIF_URL)
        interaction.app_permissions.embed_links = False
        await command.callback(interaction)
        interaction.response.send_message.assert_awaited_with(IMPORTANT_GIF_URL)


class PlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_autocomplete_queues_the_selected_video(self):
        track = Track("abcdefghijk", "Basket Case", "Green Day", 182)
        with patch.object(bot.youtube, "search", AsyncMock(return_value=[track])):
            choices = await match_songs(None, "green day")
        self.assertEqual(choices[0].name, "Basket Case - Green Day [03:02]")
        self.assertEqual(parse_query(choices[0].value).id, track.id)
        self.assertEqual(parse_query("https://youtu.be/abcdefghijk?t=30").id, track.id)
        with self.assertRaises(MusicError):
            parse_query("https://example.com/watch?v=abcdefghijk")

    async def test_skip_during_lookup_and_play_the_next_track(self):
        track = Track("abcdefghijk", "Second song")
        lookup_started = asyncio.Event()

        async def slow_search(query):
            lookup_started.set()
            await asyncio.Future()

        youtube = SimpleNamespace(search=slow_search, stream=AsyncMock(return_value=("Second song", "", {})))
        channel = SimpleNamespace(send=AsyncMock())
        voice = Mock()
        voice.disconnect = AsyncMock()
        player = Player(voice, youtube)
        first_frame = Mock()

        def make_source(url, headers, on_start):
            first_frame.side_effect = on_start
            return Mock()

        def complete(source, *, after):
            first_frame()
            after(None)

        voice.play.side_effect = complete
        with patch("bot.audio_source", side_effect=make_source):
            player.add(Request("slow song", channel, 1, 100))
            player.add(Request(track, channel, 2, 101))
            await asyncio.wait_for(lookup_started.wait(), 1)
            self.assertTrue(player.skip())
            await asyncio.wait_for(player.worker, 1)
        voice.play.assert_called_once()
        self.assertIn("Second song", channel.send.await_args.args[0])
        controls = channel.send.await_args.kwargs["view"]
        self.assertEqual([item.label for item in controls.children], ["Pause / Resume", "Skip", "Queue"])
        channel.send.return_value.edit.assert_awaited_once_with(view=None)
        self.assertFalse(player.skip())
        await player.close()
        voice.disconnect.assert_awaited_once()

    async def test_only_real_audio_frames_trigger_playing(self):
        source = object.__new__(VoiceAudio)
        source.cleanup = Mock()
        source.started = False
        source.on_start = Mock()
        with patch("discord.FFmpegOpusAudio.read", side_effect=[b"OpusHead", b"OpusTags", b"frame", b""]):
            self.assertEqual(source.read(), b"frame")
            self.assertEqual(source.read(), b"")
        source.on_start.assert_called_once()
        source.started = False
        with patch("discord.FFmpegOpusAudio.read", return_value=b""):
            with self.assertRaises(OSError):
                source.read()


class ControlsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.channel = object()
        self.voice = Mock(channel=self.channel)
        self.player = Player(self.voice, Mock())
        self.interaction = SimpleNamespace(
            client=SimpleNamespace(players={7: self.player}), guild_id=7,
            user=SimpleNamespace(id=1, voice=SimpleNamespace(channel=self.channel)),
            response=AsyncMock(),
        )

    async def test_buttons_only_control_current_song_in_same_channel(self):
        track = Request("song", None, 1, 100)
        self.player.current = track
        self.player.playing = Mock()
        self.player.playing.done.return_value = False
        self.player.playing.cancelling.return_value = 0
        controls = PlaybackControls(self.player, track)
        self.assertTrue(await controls.interaction_check(self.interaction))
        self.voice.is_paused.return_value = False
        self.voice.is_playing.return_value = True
        await controls.pause_resume.callback(self.interaction)
        self.voice.pause.assert_called_once()
        self.voice.is_paused.return_value = True
        await controls.pause_resume.callback(self.interaction)
        self.voice.resume.assert_called_once()
        await controls.queue.callback(self.interaction)
        self.assertTrue(self.interaction.response.send_message.await_args.kwargs["ephemeral"])
        await controls.skip.callback(self.interaction)
        self.player.playing.cancel.assert_called_once()
        self.interaction.user.voice = None
        self.assertFalse(await controls.interaction_check(self.interaction))
        self.interaction.user.voice = SimpleNamespace(channel=self.channel)
        self.player.current = Request("song", None, 1, 101)
        self.assertFalse(await controls.interaction_check(self.interaction))

    async def test_undo_checks_owner_and_stable_request_across_pages(self):
        self.player.queue.extend(Request("same title", None, i % 2 + 1, 100 + i) for i in range(12))
        view = QueueView(self.player, 1)
        first_page = view.render()
        self.assertIn("<@1>", first_page["content"])
        self.assertIn("<@2>", first_page["content"])
        self.assertEqual(len(view.remove_request.options), 5)
        await view.next_page.callback(self.interaction)
        self.assertEqual([item.value for item in view.remove_request.options], ["110"])
        def select(value):
            view.remove_request._refresh_state(self.interaction, {"values": [str(value)]})
        self.player.queue.popleft()  # Other songs can advance after the dropdown opens.
        select(110)
        await view.remove_request.callback(self.interaction)
        remaining = [request.id for request in self.player.queue]
        self.assertNotIn(110, remaining)
        self.assertIn(111, remaining)
        select(111)  # Another listener's request.
        await view.remove_request.callback(self.interaction)
        select(110)  # Repeated click on own stale request.
        await view.remove_request.callback(self.interaction)
        self.assertEqual([request.id for request in self.player.queue], remaining)
        self.assertEqual(view.page, 0)


class EmptyChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejoin_cancels_timer_then_second_departure_clears_queue(self):
        guild = SimpleNamespace(id=1234, me=SimpleNamespace(id=10),
                                get_member=lambda user_id: SimpleNamespace(bot=user_id == 10))
        channel = SimpleNamespace(guild=guild, voice_states={10: None}, id=12)
        inside, outside = SimpleNamespace(channel=channel), SimpleNamespace(channel=None)
        guild.me.voice = inside
        listener = SimpleNamespace(id=20, bot=False, guild=guild)
        player = Player(SimpleNamespace(channel=channel, disconnect=AsyncMock()), Mock())
        player.queue.append(Request("waiting song", None, 20, 100))
        with patch.dict(bot.players, {guild.id: player}), patch("bot.EMPTY_CHANNEL_SECONDS", 0):
            await bot.on_voice_state_update(listener, inside, outside)
            timer = player.empty_timer
            bot.watch_empty_channel(player, channel)
            self.assertIs(timer, player.empty_timer)
            channel.voice_states[20] = None
            await bot.on_voice_state_update(listener, outside, inside)
            await asyncio.gather(timer, return_exceptions=True)
            self.assertIsNone(player.empty_timer)
            self.assertEqual(len(player.queue), 1)
            player.voice.disconnect.assert_not_awaited()
            del channel.voice_states[20]
            await bot.on_voice_state_update(listener, inside, outside)
            await asyncio.wait_for(player.empty_timer, 1)
            self.assertNotIn(guild.id, bot.players)
            self.assertFalse(player.queue)
            player.voice.disconnect.assert_awaited_once_with(force=True)

    async def test_expiring_timer_cannot_evict_replacement_player(self):
        guild = SimpleNamespace(id=1235, me=Mock(), get_member=Mock())
        channel = SimpleNamespace(guild=guild, voice_states={})
        guild.me.voice.channel = channel
        old = Player(SimpleNamespace(disconnect=AsyncMock()), Mock())
        replacement = object()
        with patch.dict(bot.players, {guild.id: replacement}), patch("bot.EMPTY_CHANNEL_SECONDS", 0):
            bot.watch_empty_channel(old, channel)
            await asyncio.wait_for(old.empty_timer, 1)
            old.voice.disconnect.assert_not_awaited()
            self.assertIs(bot.players[guild.id], replacement)


if __name__ == "__main__":
    unittest.main()
