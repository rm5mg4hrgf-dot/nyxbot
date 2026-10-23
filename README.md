# nyxbot# Mafia Master Discord Bot

https://discord.com/oauth2/authorize?client_id=1548731472146337822&permissions=120259095552&integration_type=0&scope=applications.commands+bot

A real `discord.py` bot for playing Mafia in a Discord server. It includes interactive lobbies, private role and night-action messages, timed discussion and voting, a players-only thread option, built-in morning stories, and SQLite recovery after a restart.

## 1. Create the Discord application

These steps work from the Discord Developer Portal in a browser on an iPad:

1. Open the Discord Developer Portal and create a **New Application**.
2. Open **Bot** and create the application's bot user.
3. Under **Privileged Gateway Intents**, enable **Server Members Intent**. To let AI players reply to normal discussion messages, also enable **Message Content Intent**. If it is not enabled, the bot automatically starts without chat-reading so slash commands and game turns remain available.
4. Copy the bot token once. Keep it private; never paste it into chat or a source file.
5. Under **OAuth2 → URL Generator**, select the `bot` and `applications.commands` scopes.
6. Give the bot these permissions:
   - View Channels
   - Send Messages
   - Embed Links
   - Read Message History
   - Create Private Threads
   - Send Messages in Threads
   - Manage Threads (needed to add players to private threads and remove game threads when the game ends)
7. Open the generated invite URL, choose your test server, and authorize it.

Use a private test server first. The bot needs its private-thread permissions to start games because Mafia members coordinate in a thread hidden from other players.

## 2. Store the token securely in Replit

1. Open this project's **Tools** pane and choose **Secrets**.
2. Add a secret with the key `DISCORD_BOT_TOKEN` and the token as its value.
3. Do not put the token in `README.md`, Python files, a chat message, or a committed `.env` file.
4. For AI morning stories, add your OpenAI API key as the `OPENAI_API_KEY` secret. This is optional: built-in stories are used if the key is absent or the API is unavailable.

The bot reads the token from the environment at launch. It does not print it or store it in SQLite.
AI player decisions and chat replies run locally and do not need an API key. Automatic replies to normal chat require **Message Content Intent** in the Discord Developer Portal; the `/mafia opinion` command also lets a living player share a theory for an AI response if that intent is unavailable. The bot reads messages only in the active game channel.

AI morning stories are separate and optional. Story requests use `gpt-5-nano` and are billed by OpenAI based on usage. The request contains only the day number and whether the already-resolved public result includes an elimination; it does not send player names, roles, or night actions. Disable stories in `/mafia configure` with `ai_storytelling:false` to use only the built-in stories.

## 3. Start the bot

In Replit, open the **Mafia Master Bot** workflow and start it. If you prefer a shell:

```bash
cd discord-bot
python -m mafia_bot
```

The first launch registers the `/start` command and the `/mafia` slash-command group. Discord may take a short time to show newly registered global commands. The bot must be running for games to work.

The SQLite database is created automatically at `discord-bot/data/mafia-master.sqlite3`. It stores lobbies, assigned roles, votes, actions, settings, deadlines, and public game history. Keep the database file if you want saved settings and active games to survive restarts.

## 4. Play a test game

1. In a server text channel, run `/mafia create`. Set `min_players` to the minimum humans (including the host) and `max_players` to the total. Example: `min_players:1 max_players:4` gives one human and three AI players.
2. Friends can select **Join**, or the host can use **Add Player** and enter a Discord mention or ID. When at least the human minimum has joined, the host starts the game; AI fills the remaining seats up to the maximum. Set minimum equal to maximum for an all-human game.
3. Alternatively, run `/start` to choose a total of 4–12 and an exact human count privately. The host is included, and friends can join before the host starts.
4. Every human player must accept direct messages from server members. Human role cards and night actions are sent privately.
5. AI players have distinct names and personalities. They speak, vote, and take available role actions using role-appropriate information; the existing game engine still assigns roles and resolves all outcomes.

Available commands:

- `/mafia create` — open a mixed lobby (`min_players` is the human minimum; `max_players` is total game size).
- `/start` — choose the total and exact human seats using private select menus.
- `/mafia configure` — change defaults for future games (requires Manage Server).
- `/mafia rules` — show the active game's rules or the server defaults.
- `/mafia status` — show public status without role or night-action details.
- `/mafia opinion` — share a theory or defense and get a role-blind AI response during Day Discussion.

Lobby controls are host-protected where appropriate. Daytime votes can be changed until the voting deadline. Night actions are private and one-time; a missing action is treated as no action when its deadline expires.

## 5. Rules and privacy

- **Beginner:** one Mafia; the remaining players are Townspeople.
- **Classic:** Mafia, Doctor, Detective, and (with larger groups) Joker roles are added by player count.
- **Custom:** configure Mafia, Doctor, Detective, and Joker counts; remaining seats become Townspeople.
- **Day tie:** no elimination or random selection.
- **Mafia tie:** no night target or random selection.
- Doctor self-protection, Joker investigation alignment, role reveal, public/anonymous voting, phase timers, and spectator access are configurable.
- Turning spectator access off makes a private game thread and adds only participants. A separate private Mafia thread is always used.
- Public announcements never include Mafia identities, hidden roles (unless the configured daytime reveal applies), private night actions, Detective results, or private ballots.
- The final screen reveals all roles and only public events; it does not reveal hidden actions.
- Built-in stories work without an AI service or paid API key.
- AI opponents do not use OpenAI and keep playing if the story API is unavailable. If Message Content Intent is disabled, normal-chat replies are off, but the bot starts and AI players still speak and play.

## 6. Run automated tests

From the project root:

```bash
cd discord-bot
python -m unittest discover -s tests -v
```

The tests exercise the game engine and SQLite persistence without logging into Discord. They do not prove that a particular server's permissions, bot invite, slash-command propagation, direct-message settings, or private-thread access are configured correctly.

## Manual Discord checks still needed

- Confirm `/start` and `/mafia` commands appear after inviting the bot.
- Enable Message Content Intent in the Developer Portal and confirm an AI replies to a question during public discussion.
- Confirm the bot can create the two private threads and add their intended members.
- Confirm the game and Mafia private threads are deleted after a win; the parent channel remains untouched.
- Confirm every participant can receive DMs and interact with the private role/action menus.
- Try a complete game in a test server, including a tied vote, Doctor save, Detective result, Joker daytime elimination, restart during a phase, and a Mafia/Town win.
- Confirm public vote mode, anonymous vote mode, and players-only spectator mode behave as your server expects.

## Project files

```text
discord-bot/
├── README.md
├── requirements.txt
├── mafia_bot/
│   ├── __main__.py
│   ├── ai_players.py
│   ├── ai_storyteller.py
│   ├── bot.py
│   ├── commands.py
│   ├── engine.py
│   ├── logging_config.py
│   ├── manager.py
│   ├── models.py
│   ├── presentation.py
│   ├── stories.py
│   ├── storage.py
│   └── ui.py
└── tests/
    ├── test_ai_players.py
    ├── test_ai_storyteller.py
    ├── test_engine.py
    ├── test_storage.py
    └── test_stories.py
```
