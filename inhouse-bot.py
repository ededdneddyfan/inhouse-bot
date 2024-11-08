#!/usr/bin/python3

import asyncio
import datetime
import discord
import json
import os
import paramiko
import random
import re
import socket
import urllib.request
from ftplib import FTP

from collections import deque
from dotenv import load_dotenv
from discord.ext import commands
from discord.ext import tasks
import zipfile
import logging
import traceback

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)

intents = discord.Intents.default()
intents.message_content = True

client = commands.Bot(
    command_prefix=["!", "+", "-"],
    help_command=None,
    case_insensitive=True,
    intents=intents,
)

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_NAME = os.getenv("DISCORD_CHANNEL")
SERVER_IP = os.getenv("SERVER_IP")
SERVER_PORT = os.getenv("SERVER_PORT")  # port to communicate with server plugin
SERVER_PASSWORD = os.getenv("SERVER_PASSWORD")
CLIENT_PORT = os.getenv(
    "CLIENT_PORT"
)  # port to communicate with client plugin listener (serverComms.py)


@client.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send("This command is on a %.2fs cooldown" % error.retry_after)
    raise error  # re-raise the error so all the errors will still show up in console


def hltv_file_handler(ssh_client):
    try:
        ftp = ssh_client.open_sftp()
        output_filename = None
        ftp.chdir("/root/.steam/steamcmd/tfc/tfc/HLTV")
        # getting lists

        HLTVListNameDesc = list(
            reversed(sorted(ftp.listdir()))
        )  # Reverse the list of logs so it's in descending order
        lastTwoBigHLTVList = []
        for HLTVFile in HLTVListNameDesc:
            size = int(ftp.stat(HLTVFile).st_size)
            # Do a simple heuristic check to see if this is a "real" round.  TODO: maybe use a smarter heuristic
            # if we find any edge cases.
            if (size > 11000000) and (
                ".dem" in HLTVFile
            ):  # Rounds with logs of players and time will be big
                print("passed heuristic!")
                lastTwoBigHLTVList.append(HLTVFile)
                if len(lastTwoBigHLTVList) >= 2:
                    break

        if len(lastTwoBigHLTVList) >= 2:
            HLTVToZip1 = lastTwoBigHLTVList[1]
            HLTVToZip2 = lastTwoBigHLTVList[0]

            # zip file stuff.. get rid of slashes so we dont error.
            split_filename = HLTVToZip1.split("-")
            pickup_date = split_filename[
                1
            ]  # Just use the time of the first round, it's good enough
            pickup_map = split_filename[2].replace(".dem", "")
            ftp.get(HLTVToZip1, HLTVToZip1)
            ftp.get(HLTVToZip2, HLTVToZip2)
            mode = zipfile.ZIP_DEFLATED
            output_filename = pickup_map + "-" + pickup_date + ".zip"
            zip = zipfile.ZipFile(output_filename, "w", mode)
            zip.write(HLTVToZip1)
            zip.write(HLTVToZip2)
            zip.close()
            ftp.close()
            os.remove(HLTVToZip1)
            os.remove(HLTVToZip2)
        return output_filename
    except Exception as e:
        logging.warning(traceback.format_exc())
        logging.warning(f"error here. {e}")
        return None


def hampalyze_logs_sftp(ssh_client):
    ftp = ssh_client.open_sftp()
    # hardcoding directory, sorry
    ftp.chdir(
        "/root/.steam/steamcmd/tfc/tfc/logs"
    )  # Navigate to the logs subfolder

    # Get the list of files in the logs folder
    logFiles = list(
        reversed(sorted(ftp.listdir()))
    )  # Reverse the list of logs so it's in descending order
    firstLog = None
    secondLog = None
    round1log = None  # Redundant, workaround for my own inadequacies
    round2log = None  # Redundant, workaround for my own inadequacies

    for logFile in logFiles:  # Just check the last few logs
        if ".log" not in logFile:
            continue
        # Log files from 4v4 games are generally over 100k bytes
        if (
            int(ftp.stat(logFile).st_size) > 50000
        ):  # Log files from 2v2 games may be more like 70k
            # Check the current file's modified time based on the unix modified timestamp
            logModified = datetime.datetime.fromtimestamp(ftp.stat(logFile).st_mtime)
            if firstLog is None:
                print(logFile + " is set to round2log")
                round2log = logFile
                firstLog = (logFile, logModified)
                continue

            # otherwise, verify that there was another round played at least <60 minutes within the last found log
            if (firstLog[1] - logModified).total_seconds() < 3600:
                round1log = logFile
                print(logFile + " is set to round1log")
                secondLog = (logFile, logModified)

            # if secondLog is not populated, this is probably the first pickup of the day; abort
            break

    # Abort if we didn't find two logs
    if firstLog is None or secondLog is None:
        print("Could not find a log")
        return

    # Retrieve first log file (most recent; round 2)
    ftp.get(round2log, round2log)

    # Retrieve second log file (round 1)
    ftp.get(round1log, round1log)
    ftp.close()

    # Send the retrieved log files to hampalyzer
    hampalyze = (
        "curl -X POST -F force=on -F logs[]=@%s -F logs[]=@%s http://app.hampalyzer.com/api/parseGame"
        % (round1log, round2log)
    )
    # Capture the result
    output = os.popen(hampalyze).read()
    print(output)

    # Check if it worked or not
    status = json.loads(output)
    if "success" in status:
        site = "http://app.hampalyzer.com" + status["success"]["path"]
        print("Parsed logs available: %s" % site)
        # not using json stuff currently
        # with open('prevlog.json', 'w') as f:
        #     prevlog = { 'site': site, 'logFiles': [ firstLog[0], secondLog[0] ] }
        #     json.dump(prevlog, f)
        os.remove(round2log)
        os.remove(round1log)
    else:
        print("error parsing logs: %s" % output)
    return site  # Give the hampalyzer link


def hampalyze_logs():
    # Connect to FTP using info from .env file
    ftp = FTP()
    ftp.connect(os.getenv("FTP_SERVER"), 21)
    ftp.login(os.getenv("FTP_USER"), os.getenv("FTP_PASSWD"))
    ftp.cwd("/logs")  # Navigate to the logs subfolder

    # Get the list of files in the logs folder
    logFiles = list(
        reversed(ftp.nlst())
    )  # Reverse the list of logs so it's in descending order
    firstLog = None
    secondLog = None
    round1log = None  # Redundant, workaround for my own inadequacies
    round2log = None  # Redundant, workaround for my own inadequacies

    for logFile in logFiles[:300]:  # Just check the last few logs
        if ".log" not in logFile:
            continue

        # not using json stuff currently
        # if 'logFiles' in prevlog and logFile in prevlog['logFiles']:
        #     print("already parsed the latest log")
        #     return

        # Log files from 4v4 games are generally over 100k bytes
        if (
            int(ftp.size(logFile)) > 50000
        ):  # Log files from 2v2 games may be more like 70k
            # Hamp's inhouse bot does this and I don't fully understand it but it works like magic - thanks hamp!
            logModified = datetime.datetime.strptime(
                ftp.voidcmd("MDTM %s" % logFile).split()[-1], "%Y%m%d%H%M%S"
            )
            if firstLog is None:
                print(logFile + " is set to round2log")
                round2log = logFile
                firstLog = (logFile, logModified)
                continue

            # otherwise, verify that there was another round played at least <60 minutes within the last found log
            if (firstLog[1] - logModified).total_seconds() < 3600:
                round1log = logFile
                print(logFile + " is set to round1log")
                secondLog = (logFile, logModified)

            # if secondLog is not populated, this is probably the first pickup of the day; abort
            break

    # Abort if we didn't find two logs
    if firstLog is None or secondLog is None:
        print("Could not find a log")
        return

    # Retrieve first log file (most recent; round 2)
    # ftp.retrbinary("RETR %s" % round2log, open('logs/%s' % round2log, 'wb').write) # Not sure why this doesn't work
    with open(round2log, "wb") as fp:  # Workaround
        print("Downloading " + round2log)
        ftp.retrbinary("RETR {0}".format(round2log), fp.write)

    # Retrieve second log file (round 1)
    # ftp.retrbinary("RETR %s" % secondLog[0], open('logs/%s' % secondLog[0], 'wb').write) # Not sure why this doesn't work
    with open(round1log, "wb") as fp:  # Workaround
        print("Downloading " + round1log)
        ftp.retrbinary("RETR {0}".format(round1log), fp.write)

    # Send the retrieved log files to hampalyzer
    hampalyze = (
        "curl -X POST -F logs[]=@%s -F logs[]=@%s http://app.hampalyzer.com/api/parseGame"
        % (round1log, round2log)
    )
    # Capture the result
    output = os.popen(hampalyze).read()
    print(output)

    # Check if it worked or not
    status = json.loads(output)
    if "success" in status:
        site = "http://app.hampalyzer.com" + status["success"]["path"]
        print("Parsed logs available: %s" % site)
        # not using json stuff currently
        # with open('prevlog.json', 'w') as f:
        #     prevlog = { 'site': site, 'logFiles': [ firstLog[0], secondLog[0] ] }
        #     json.dump(prevlog, f)
    else:
        print("error parsing logs: %s" % output)

    return site  # Give the hampalyzer link


# on load, load previous teams + map from the prev* files
if os.path.exists("prevmaps.json"):
    with open("prevmaps.json", "r") as f:
        previousMaps = deque(json.load(f), maxlen=5)
else:
    previousMaps = []

if os.path.exists("prevteams.json"):
    with open("prevteams.json", "r") as f:
        previousTeam = json.load(f)
else:
    previousTeam = []

emoji = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣"]
mapList = []

playerList = {}
pickupStarted = False
pickupActive = False
playerNumber = 8
lastAdd = datetime.datetime.utcnow()
lastAddCtx = None

mapChoices = []

recentlyPlayedMapsMsg = None
mapVote = False
mapVoteMessage = None
mapVoteMessageView = None
nextCancelConfirms = False


class MapChoice:
    def __init__(self, mapName, decoration=None):
        self.mapName = mapName
        self.decoration = decoration
        self.votes = []

    # maybe other voting methods here?


async def HandleMapButtonCallback(
    self, interaction: discord.Interaction, button: discord.ui.Button
):
    global mapVoteMessage
    if self is mapVoteMessageView:
        processVote(interaction.user, int(button.custom_id))
        await interaction.response.edit_message(embed=GenerateMapVoteEmbed())


class MapChoiceView(discord.ui.View):
    def __init__(self, mapChoices):
        super().__init__()
        self.addButtons()

    def addButtons(self):
        global emoji
        for idx, mapChoice in enumerate(mapChoices):
            self.add_item(
                self.createButton(
                    label=f"{emoji[idx]} {mapChoice.mapName}", custom_id=f"{idx + 1}"
                )
            )

    def createButton(self, label, custom_id):
        button = discord.ui.Button(label=label, custom_id=custom_id)

        async def mapButtonCallback(interaction: discord.Interaction):
            await HandleMapButtonCallback(self, interaction, button)

        button.callback = mapButtonCallback
        return button


# @debounce(2)
async def printPlayerList(ctx):
    global playerList
    global playerNumber

    msg = ", ".join([s for s in playerList.values()])
    counter = str(len(playerList)) + "/" + str(playerNumber)

    await ctx.send("```\nPlayers (" + counter + ")\n" + msg + "```")
    await updateNick(ctx, counter)


async def DePopulatePickup(ctx):
    global pickupStarted
    global pickupActive
    global playerNumber
    global mapVote
    global playerList

    mapVote = False
    pickupStarted = False
    pickupActive = False
    playerNumber = 8
    playerList = {}

    if idlecancel.is_running():
        idlecancel.stop()

    if ctx:
        await updateNick(ctx)


def PickMaps(initial=False):
    global mapList
    global mapChoices

    mapChoices = []
    if initial:
        for i in range(6):
            if i == 0:
                mapname = random.choice(mapList["tier1"] + mapList["tier2"])
                RemoveMap(mapname)
                mapChoices.append(MapChoice(mapname))
            elif i == 1:
                mapname = random.choice(mapList["tier2"] + mapList["tier3"])
                RemoveMap(mapname)
                mapChoices.append(MapChoice(mapname))
            elif i == 2:
                mapname = random.choice(mapList["tier3"])
                RemoveMap(mapname)
                mapChoices.append(MapChoice(mapname))
            elif i == 3:
                mapname = random.choice(mapList["tier1"] + mapList["tier2"])
                RemoveMap(mapname)
                mapChoices.append(MapChoice(mapname))
            elif i == 4:
                mapname = random.choice(mapList["tier2"] + mapList["tier3"])
                RemoveMap(mapname)
                mapChoices.append(MapChoice(mapname))
            elif i == 5:
                mapname = random.choice(mapList["tier3"])
                RemoveMap(mapname)
                mapChoices.append(MapChoice(mapname))
    else:
        for i in range(6):
            if i == 0:
                mapname = random.choice(mapList["tier1"] + mapList["tier2"])
                RemoveMap(mapname)
                mapChoices.append(MapChoice(mapname))
            elif i == 1:
                mapname = random.choice(mapList["tier1"] + mapList["tier2"])
                RemoveMap(mapname)
                mapChoices.append(MapChoice(mapname))
            elif i == 2:
                mapname = random.choice(mapList["tier1"] + mapList["tier2"])
                RemoveMap(mapname)
                mapChoices.append(MapChoice(mapname))
            elif i == 3:
                mapname = random.choice(mapList["tier1"] + mapList["tier2"])
                RemoveMap(mapname)
                mapChoices.append(MapChoice(mapname))
            elif i == 4:
                mapname = random.choice(mapList["tier3"] + mapList["tier1"] + mapList["tier2"])
                RemoveMap(mapname)
                mapChoices.append(MapChoice(mapname))
            elif i == 5:
                mapname = random.choice(mapList["tier3"] + mapList["tier1"] + mapList["tier2"])
                RemoveMap(mapname)
                mapChoices.append(MapChoice(mapname))

def RemoveMap(givenMap):
    global mapList

    if givenMap in mapList["tier1"]:
        mapList["tier1"].remove(givenMap)
    elif givenMap in mapList["tier2"]:
        mapList["tier2"].remove(givenMap)
    elif givenMap in mapList["tier3"]:
        mapList["tier3"].remove(givenMap)


def RecordMapAndTeams(winningMap):
    global previousMaps
    global playerList
    global previousTeam

    previousMaps.append(winningMap)
    with open("prevmaps.json", "w") as f:
        json.dump(list(previousMaps), f)

    previousTeam = list(playerList.values())
    with open("prevteams.json", "w") as f:
        json.dump(previousTeam, f)


async def updateNick(ctx, status=None):
    if status == "" or status is None:
        status = None
    else:
        status = "ETFC (" + status + ")"

    await ctx.message.guild.me.edit(nick=status)


@client.command(pass_context=True)
async def pickup(ctx):
    global pickupStarted
    global pickupActive
    global mapVote
    global mapList
    global playerNumber
    global previousMaps
    global recentlyPlayedMapsMsg
    global nextCancelConfirms

    if (
        pickupStarted == False
        and pickupActive == False
        and mapVote == False
        and ctx.channel.name == CHANNEL_NAME
    ):
        with open("maplist.json") as f:
            mapList = json.load(f)
            for prevMap in previousMaps:
                for tier in mapList.values():
                    if prevMap in tier:
                        tier.remove(prevMap)

        DePopulatePickup

        pickupStarted = True
        nextCancelConfirms = False
        recentlyPlayedMapsMsg = (
            "Maps %s were recently played and are removed from voting."
            % ", ".join(previousMaps)
        )

        await ctx.send("Pickup started. !add in 10 seconds")
        await updateNick(ctx, "starting...")
        await asyncio.sleep(5)
        await ctx.send("!add in 5 seconds")
        await asyncio.sleep(5)

        if pickupStarted == True:
            pickupActive = True
            await ctx.send("!add enabled")
            await printPlayerList(ctx)
        else:
            await ctx.send("Pickup was canceled before countdown finished.")


@client.command(pass_context=True)
async def cancel(ctx):
    global pickupStarted
    global pickupActive
    global mapVote
    global mapVoteMessage
    global nextCancelConfirms

    if mapVote != False and not nextCancelConfirms:
        await ctx.send("You're still picking maps, still want to cancel?")
        nextCancelConfirms = True
        return
    if pickupStarted == True or pickupActive == True:
        pickupStarted = False
        pickupActive = False
        if mapVoteMessage is not None:
            await mapVoteMessage.edit(view=None)
            mapVoteMessage = None
        await ctx.send("Pickup canceled.")
        await DePopulatePickup(ctx)
    else:
        await ctx.send("No pickup active.")


@client.command(pass_context=True)
async def playernumber(ctx, numPlayers: int):
    global playerNumber

    if ctx.channel.name != CHANNEL_NAME:
        return

    try:
        players = int(numPlayers)
    except:
        await ctx.send("Given value isn't a number.")
        return

    if players % 2 == 0 and players <= 20 and players >= 2:
        playerNumber = players
        await ctx.send("Set pickup to fill at %d players" % playerNumber)
        await updateNick(ctx, str(len(playerList)) + "/" + str(playerNumber))
    else:
        await ctx.send(
            "Can't set pickup to an odd number, too few, or too many players"
        )


def GenerateMapVoteEmbed():
    global emoji
    global mapChoices
    global recentlyPlayedMapsMsg

    embed = discord.Embed(
        title="Vote for your map!",
        description="When vote is stable, !lockmap",
        color=0x00FFFF,
    )

    for i in range(len(mapChoices)):
        mapChoice = mapChoices[i]
        mapName = mapChoice.mapName
        decoration = mapChoice.decoration or ""

        votes = mapChoice.votes
        numVotes = len(votes)
        whoVoted = ", ".join([playerList[playerId] for playerId in votes])
        whoVotedString = whoVoted
        if len(whoVoted) > 0:
            whoVotedString = "_" + whoVotedString + "_"

        if numVotes == 1:
            voteCountString = "1 vote"
        else:
            voteCountString = "%d votes" % (numVotes)

        embed.add_field(
            name="",
            value=emoji[i]
            + " `"
            + mapName
            + " "
            + decoration
            + (" " * (25 - len(mapName) - 2 * len(decoration)))
            + voteCountString
            + "`\n\u200b"
            + whoVotedString,
            inline=False,
        )

    if recentlyPlayedMapsMsg != None:
        embed.add_field(name="", value=recentlyPlayedMapsMsg, inline=False)

    playersVoted = [
        playerId for mapChoice in mapChoices for playerId in mapChoice.votes
    ]
    playersAbstained = [
        playerList[playerId]
        for playerId in playerList.keys()
        if playerId not in playersVoted
    ]
    if len(playersAbstained) != 0 and len(playersAbstained) != len(playerList):
        embed.add_field(
            name="",
            value="```"
            + ", ".join(playersAbstained)
            + " need"
            + ("s" if len(playersAbstained) == 1 else "")
            + " to vote```",
            inline=False,
        )

    return embed


@client.command(pass_context=True, name="+")
async def plusPlus(ctx):
    if ctx.prefix == "+":
        await add(ctx)


@client.command(pass_context=True, name="-")
async def minusMinus(ctx):
    if ctx.prefix == "-":
        await remove(ctx)


@client.command(pass_context=True)
async def add(ctx):
    global playerNumber
    global playerList
    global pickupActive
    global mapVote
    global mapVoteMessage
    global mapVoteMessageView
    global previousMaps
    global lastAdd
    global lastAddCtx

    global mapChoices

    player = ctx.author

    if pickupActive == True and ctx.channel.name == CHANNEL_NAME:
        playerId = player.id
        playerName = player.display_name
        if playerId not in playerList:
            playerList[playerId] = playerName
            lastAdd = datetime.datetime.utcnow()

            if not idlecancel.is_running():
                idlecancel.start()
                lastAddCtx = ctx

            if len(playerList) < playerNumber:
                await printPlayerList(ctx)
            else:
                pickupActive = False
                if idlecancel.is_running():
                    idlecancel.stop()

                await printPlayerList(ctx)
                await updateNick(ctx, "voting...")

                # ensure that playerlist is first n people added
                playerList = dict(list(playerList.items())[:playerNumber])

                PickMaps(True)
                mapChoices.append(MapChoice("New Maps"))

                mapVote = True

                embed = GenerateMapVoteEmbed()
                mapVoteMessageView = MapChoiceView(mapChoices)
                mapVoteMessage = await ctx.send(embed=embed, view=mapVoteMessageView)

                mentionString = ""
                for playerId in playerList.keys():
                    mentionString = mentionString + ("<@%s> " % playerId)
                await ctx.send(mentionString)


@tasks.loop(minutes=30)
async def idlecancel():
    global lastAdd
    global lastAddCtx
    global pickupActive
    global mapVote

    if pickupActive == True and pickupStarted == True and mapVote == False:
        # check if 3 hours since last add
        lastAddDiff = (datetime.datetime.utcnow() - lastAdd).total_seconds()
        print("last add was %d minutes ago" % (lastAddDiff / 60))

        if lastAddDiff > (3 * 60 * 60):
            print("stopping pickup")

            await lastAddCtx.send("Pickup idle for more than three hours, canceling.")
            await DePopulatePickup(lastAddCtx)


@client.command(pass_context=True)
async def remove(ctx):
    global playerList
    global pickupActive

    if pickupActive == True and ctx.channel.name == CHANNEL_NAME:
        if ctx.author.id in playerList:
            del playerList[ctx.author.id]
            await printPlayerList(ctx)


@client.command(pass_context=True)
@commands.has_role("admin")
async def kick(ctx, player: discord.User):
    global playerList

    if player is not None and player.id in playerList:
        del playerList[player.id]
        await ctx.send("Kicked %s from the pickup." % player.mention)
        await printPlayerList(ctx)


@client.command(pass_context=True)
async def teams(ctx):
    if ctx.channel.name != CHANNEL_NAME:
        return

    if pickupStarted == False:
        await ctx.send("No pickup active.")
    else:
        await printPlayerList(ctx)


def processVote(player: discord.Member = None, vote=None):
    global mapVote
    global playerList

    global mapChoices

    if player.id in playerList:
        # remove any existing votes
        for mapChoice in mapChoices:
            if player.id in mapChoice.votes:
                mapChoice.votes.remove(player.id)

        mapChoices[vote - 1].votes.append(player.id)


@client.command(pass_context=True, aliases=["fv"])
async def lockmap(ctx):
    global mapVote
    global mapVoteMessage
    global mapVoteMessageView

    global mapChoices

    global mapList
    global previousMaps
    global recentlyPlayedMapsMsg
    global nextCancelConfirms

    if ctx.channel.name != CHANNEL_NAME:
        return

    rankedVotes = []
    highestVote = 0
    winningMap = " "

    if mapVote == True:
        nextCancelConfirms = False

        # get top maps
        mapTally = [
            (mapChoice.mapName, len(mapChoice.votes)) for mapChoice in mapChoices
        ]
        rankedVotes = sorted(mapTally, key=lambda e: e[1], reverse=True)

        highestVote = rankedVotes[0][1]

        # don't allow lockmap if no votes were cast
        if highestVote == 0:
            await ctx.send("!lockmap denied; no votes were cast.")
            return

        # Hide voting buttons now that the vote is complete.
        mapVoteMessageView = None
        await mapVoteMessage.edit(view=None)

        winningMaps = [
            pickedMap for (pickedMap, votes) in rankedVotes if votes == highestVote
        ]

        # don't allow "New Maps" to win
        if len(winningMaps) > 1 and "New Maps" in winningMaps:
            winningMap = "New Maps"
        else:
            winningMap = random.choice(winningMaps)

        if winningMap == "New Maps":
            PickMaps()
            carryOverMap = random.choice(
                [
                    pickedMap
                    for (pickedMap, votes) in rankedVotes
                    if votes == rankedVotes[1][1] and pickedMap != "New Maps"
                ]
            )
            mapChoices.append(MapChoice(carryOverMap, "🔁"))

            recentlyPlayedMapsMsg = None
            embed = GenerateMapVoteEmbed()
            mapVoteMessageView = MapChoiceView(mapChoices)

            mapVoteMessage = await ctx.send(embed=embed, view=mapVoteMessageView)
        else:
            mapVoteMessage = None
            mapVoteMessageView = None

            mapVote = False
            RecordMapAndTeams(winningMap)

            await ctx.send("The winning map is: " + winningMap)
            await ctx.send("Please join the server: https://shorturl.at/QYOl9")
            await ctx.send(f"connect {SERVER_IP}:27015;password " + SERVER_PASSWORD)
            await DePopulatePickup(ctx)


@client.command(pass_context=True)
async def vote(ctx):
    global mapVote
    global playerList
    global mapChoices

    if mapVote == True and ctx.channel.name == CHANNEL_NAME:
        playersVoted = [
            playerId for mapChoice in mapChoices for playerId in mapChoice.votes
        ]
        playersAbstained = [
            playerId for playerId in playerList.keys() if playerId not in playersVoted
        ]

        mentionString = "Please vote for maps: "
        for playerId in playersAbstained:
            mentionString = mentionString + ("<@%s> " % playerId)
        await ctx.send(mentionString + "")


@client.command(pass_context=True)
async def lockset(ctx, mapToLockset):
    global previousMaps
    global pickupActive
    global mapVote

    if ctx.channel.name != CHANNEL_NAME:
        return

    if pickupActive != False and mapVote != False:
        await ctx.send(
            "Error: can only !lockset during map voting or if no pickup is active (changes the map for the last pickup)."
        )
        return

    previousMaps.pop()
    previousMaps.append(mapToLockset)

    with open("prevmaps.json", "w") as f:
        json.dump(list(previousMaps), f)

    await ctx.send("Set pickup map to %s" % mapToLockset)


@client.command(pass_context=True)
async def timeleft(ctx):
    if ctx.channel.name != CHANNEL_NAME:
        return

    # construct a UDP packet and send it to the server
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto("BOT_MSG@TIMELEFT@".encode(), (SERVER_IP, int(SERVER_PORT)))

    await asyncio.sleep(3)
    if os.path.exists("timeleft.json"):
        with open("timeleft.json", "r") as f:
            try:
                timeleft = json.load(f)
                if timeleft is not None and timeleft["timeleft"]:
                    await ctx.send("Timeleft: %s" % timeleft["timeleft"])
                    return
            except:
                await ctx.send("Server did not respond.")
    else:
        await ctx.send("Server did not respond.")


"""@client.command(pass_context=True)
 async def stats(ctx):
    with open('prevlog.json', 'r') as f:
        prevlog = json.load(f)
        await ctx.send('Stats: %s' % prevlog['site'])"""


@client.command(pass_context=True)
@commands.cooldown(1, 30, commands.BucketType.user)
async def forcestats(ctx):
    print("forcestats -- channel name" + ctx.channel.name)
    if ctx.channel.name == "pickup":
        await ctx.send("force-parsing stats; wait 5 sec...")

        with open("prevlog.json", "w") as f:
            f.write("[]")

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto("BOT_MSG@END".encode(), ("0.0.0.0", int(CLIENT_PORT)))

        await asyncio.sleep(5)

        with open("prevlog.json", "r") as f:
            prevlog = json.load(f)
            await ctx.send("Stats: %s" % prevlog["site"])


@client.command(pass_context=True)
async def hltv(ctx):
    ssh_client = paramiko.SSHClient()
    ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh_client.connect(
        hostname=os.getenv("FTP_SERVER"),
        port=22,
        username=os.getenv("FTP_USER"),
        password=os.getenv("FTP_PASSWD"),
    )
    output_zipfile = hltv_file_handler(ssh_client)
    await ctx.send(file=discord.File(output_zipfile), content="HLTV Here")
    os.remove(output_zipfile)
    ssh_client.close()


@client.command(pass_context=True)
async def logs(ctx):
    await ctx.send("Logs: currently not operational, check back later!")


@client.command(pass_context=True)
async def tfcmap(ctx, map):
    map = map.lower()
    with urllib.request.urlopen(r"http://mrclan.com/tfcmaps/") as mapIndex:
        response = mapIndex.read().decode("utf-8")
        matches = re.findall('<a href="/tfcmaps/%s.zip' % (map), response, re.I)
        if len(matches) != 0:
            await ctx.send("Found map: http://mrclan.com/tfcmaps/%s.zip" % (map))
        else:
            await ctx.send(
                "Didn't find specified map. [All known maps are here](http://mrclan.com/tfcmaps/)."
            )


@client.command(pass_context=True)
async def server(ctx):
    await ctx.send("steam://connect/95.179.239.153:27015/%s" % SERVER_PASSWORD)


@client.command(pass_context=True)
async def help(ctx):
    await ctx.send("pickup: !pickup !add !remove !teams !lockmap !cancel")
    await ctx.send("info: !stats !timeleft !hltv !logs !tfcmap !server")
    await ctx.send("admin: !playernumber !kick !lockset !forcestats !vote")


# retrieve logs from FTP and get hampalyzer link
@client.command(
    name="stats", help="Hamaplyze most recent pair of large log files from FTP."
)
@commands.cooldown(1, 30, commands.BucketType.user)
async def get_logs(ctx):
    # Connect to FTP using info from .env file
    # Check if the server connection uses SFTP or FTP
    stats_channel = await client.fetch_channel(1249752385476235376)
    try:
        ssh_client = paramiko.SSHClient()
        ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh_client.connect(
            hostname=os.getenv("FTP_SERVER"),
            port=22,
            username=os.getenv("FTP_USER"),
            password=os.getenv("FTP_PASSWD"),
        )
        logs_link = hampalyze_logs_sftp(ssh_client)
        output_zipfile = hltv_file_handler(ssh_client)
        if output_zipfile is not None:
            await stats_channel.send(file=discord.File(output_zipfile), content=logs_link)
            os.remove(output_zipfile)
        ssh_client.close()
    except paramiko.ssh_exception.NoValidConnectionsError:
        # Assumption: If SFTP connection failed, try FTP instead
        logs_link = hampalyze_logs()
        await stats_channel.send(logs_link)
    except paramiko.ssh_exception.AuthenticationException:
        logs_link = hampalyze_logs()
        await stats_channel.send(logs_link)
    """if output_zipfile is not None:
        await stats_channel.send(file=discord.File(output_zipfile), content=logs_link)
        os.remove(output_zipfile)
    else:
        await stats_channel.send(logs_link)
    """

@client.event
async def on_ready():
    print(f"{client.user} is aliiiiiive!")


client.run(TOKEN)
