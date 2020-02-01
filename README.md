# Fireside Bot

***
This is a bot made for the Fireside Politics server.
Formerly known as Coffee and Politics, created right after the great purge.
***

## Getting Started

### Prerequisites
You are going to need the following:  

* [Poetry](https://python-poetry.org/docs/#installation)  
* Python 3.8+
* (Preferably) A *NIX based system

The bot is coded and tested on *NIX based systems.

### Installing
1. **Make sure to run Python 3.8 or higher.**  

This is required to actually run the bot.

2. **Set up the virtualenv and install dependencies.** 
   
The project is using [Poetry](https://python-poetry.org/) as its dependency manager.

Once poetry is installed, simply run `poetry install` and you're all set.

3. **Setup configuration**  

The next step is just to create a `config.py` file in the root directory of
the bot with the following template:

```py
token = "" # Your bot's token.
autoload = ["cogs", "to", "load"] # List of cogs to load on start-up.
```

4. **Create the database**  

For security, it is recommended you create an extra user with a
password.  

```sh
CREATE ROLE fbot WITH LOGIN PASSWORD 'yourpw';
CREATE DATABASE firesidebot OWNER fbot;
```
This will create a new database that the bot can modify.

5. **Initialise the database**  

The database initialisation command will fully set up your database.  
`poetry run python3 run.py db init`  

### Tests
To test the bot is running correctly, do .ping in your server. The response should be "Pong" from the bot in your server.

## Built With
* [Discord.py](https://github.com/Rapptz/discord.py)
