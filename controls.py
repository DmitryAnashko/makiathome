import discord


class VoiceControls(discord.ui.View):
    def __init__(self, player, *, timeout):
        super().__init__(timeout=timeout)
        self.player = player

    async def interaction_check(self, interaction):
        active = interaction.client.players.get(interaction.guild_id)
        voice = getattr(interaction.user, "voice", None)
        if active is not self.player:
            message = "This listening session has ended."
        elif not voice or voice.channel != self.player.voice.channel:
            message = "Join Maki's voice channel to use these controls."
        else:
            return True
        await interaction.response.send_message(message, ephemeral=True)
        return False


class PlaybackControls(VoiceControls):
    def __init__(self, player, request):
        super().__init__(player, timeout=None)
        self.request = request

    async def interaction_check(self, interaction):
        if not await super().interaction_check(interaction):
            return False
        task = self.player.playing
        if self.player.current is self.request and task and not task.done() and not task.cancelling():
            return True
        await interaction.response.send_message("That song has finished.", ephemeral=True)
        return False

    @discord.ui.button(label="Pause / Resume", style=discord.ButtonStyle.primary)
    async def pause_resume(self, interaction, button):
        voice = self.player.voice
        if voice.is_paused():
            voice.resume()
            message = "Resumed."
        elif voice.is_playing():
            voice.pause()
            message = "Paused."
        else:
            message = "That song has finished."
        await interaction.response.send_message(message, ephemeral=True)

    @discord.ui.button(label="Skip")
    async def skip(self, interaction, button):
        skipped = self.player.skip()
        await interaction.response.send_message("Skipped." if skipped else "That song has finished.", ephemeral=True)

    @discord.ui.button(label="Queue")
    async def queue(self, interaction, button):
        await show_queue(interaction, self.player)


class QueueView(VoiceControls):
    page_size = 10

    def __init__(self, player, owner_id):
        super().__init__(player, timeout=180)
        self.owner_id = owner_id
        self.page = 0

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Open your own queue with /queue.", ephemeral=True)
            return False
        return await super().interaction_check(interaction)

    def render(self, feedback=None):
        waiting = list(self.player.queue)
        pages = max(1, (len(waiting) + self.page_size - 1) // self.page_size)
        self.page = max(0, min(self.page, pages - 1))
        start = self.page * self.page_size
        self.clear_items()
        lines = [feedback] if feedback else []
        current = self.player.current
        if current:
            lines.append(f"Current: {discord.utils.escape_markdown(current.label[:100])} · <@{current.requester_id}>")
        choices = []
        for position, request in enumerate(waiting[start:start + self.page_size], start + 1):
            label = request.label.replace("\n", " ")[:70]
            lines.append(f"{position}. {discord.utils.escape_markdown(label)[:100]} · <@{request.requester_id}>")
            if request.requester_id == self.owner_id:
                choices.append(discord.SelectOption(label=f"{position}. {label}", value=str(request.id)))
        if choices:
            self.remove_request.options = choices
            self.add_item(self.remove_request)
        if pages > 1:
            lines.append(f"Page {self.page + 1} of {pages}")
            self.previous.disabled = self.page == 0
            self.next_page.disabled = self.page == pages - 1
            self.add_item(self.previous)
            self.add_item(self.next_page)
        if not waiting and not current:
            lines.append("The queue is empty. Add a song with /play.")
        return dict(content="\n".join(lines), view=self if self.children else None,
                    allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.select(placeholder="Remove one of my requests", row=0)
    async def remove_request(self, interaction, select):
        request_id = int(select.values[0])
        for request in self.player.queue:
            if request.id == request_id and request.requester_id == interaction.user.id:
                self.player.queue.remove(request)
                feedback = f"Removed **{discord.utils.escape_markdown(request.label[:70])}**."
                break
        else:
            feedback = "That request is no longer in your upcoming songs."
        await interaction.response.edit_message(**self.render(feedback))

    @discord.ui.button(label="Previous", row=1)
    async def previous(self, interaction, button):
        self.page -= 1
        await interaction.response.edit_message(**self.render())

    @discord.ui.button(label="Next", row=1)
    async def next_page(self, interaction, button):
        self.page += 1
        await interaction.response.edit_message(**self.render())


async def show_queue(interaction, player):
    view = QueueView(player, interaction.user.id)
    await interaction.response.send_message(ephemeral=True, **view.render())
