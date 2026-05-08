from __future__ import print_function
import pickle
import os.path
import io
import os
import time
import json
import random
import subprocess
import sys
import shutil
import requests
import http.client
import httplib2
from mimetypes import MimeTypes
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from googleapiclient.errors import HttpError
from google.auth.exceptions import RefreshError
from moviepy.editor import *

from pyfzf.pyfzf import FzfPrompt
import whiptail as wt

################################################################################
# YOUTUBE STUFF
# Explicitly tell the underlying HTTP transport library not to retry, since
# we are handling retry logic ourselves.

httplib2.RETRIES = 1

# Maximum number of times to retry before giving up.
MAX_RETRIES = 10

# Always retry when these exceptions are raised.
RETRIABLE_EXCEPTIONS = (
    httplib2.HttpLib2Error,
    IOError,
    http.client.NotConnected,
    http.client.IncompleteRead,
    http.client.ImproperConnectionState,
    http.client.CannotSendRequest,
    http.client.CannotSendHeader,
    http.client.ResponseNotReady,
    http.client.BadStatusLine,
)

# Always retry when an apiclient.errors.HttpError with one of these status
# codes is raised.
RETRIABLE_STATUS_CODES = [500, 502, 503, 504]
################################################################################
FZF_ARGS = "--margin 1,7 --border --tiebreak=begin --color fg:252,bg:237,hl:11,fg+:238,bg+:139,hl+:0 --color info:108,prompt:109,spinner:108,pointer:168,marker:168"
DISK_USAGE_THRESHOLD = 0.8


class DriveAPI:
    global SCOPES

    # Define the scopes
    SCOPES = [
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/youtube",
    ]

    def __init__(self):

        self.slack_hook = None
        self.parent_folder = None
        # grab variables from secrets file
        if not os.path.exists("secrets.json"):
            exit("Missing 'secrets.json'")

        with open("secrets.json", "r") as secrets_file:
            secrets_data = json.load(secrets_file)
            self.slack_hook = secrets_data.get("slack_hook")
            self.parent_folder = secrets_data.get("parent_folder")

        # Variable self.creds will
        # store the user access token.
        # If no valid token found
        # we will create one.
        self.creds = None

        # The file token.pickle stores the
        # user's access and refresh tokens. It is
        # created automatically when the authorization
        # flow completes for the first time.

        # Check if file token.pickle exists
        if os.path.exists("token.pickle"):

            # Read the token from the file and
            # store it in the variable self.creds
            with open("token.pickle", "rb") as token:
                self.creds = pickle.load(token)

        # If no valid credentials are available,
        # request the user to log in.
        if not self.creds or not self.creds.valid:

            # If token is expired, it will be refreshed,
            # else, we will request a new one.
            if self.creds and self.creds.expired and self.creds.refresh_token:
                self.creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    "credentials.json", SCOPES
                )
                self.creds = flow.run_local_server(port=0)

            # Save the access token in token.pickle
            # file for future usage
            with open("token.pickle", "wb") as token:
                pickle.dump(self.creds, token)

        # create fzfprompt object for later use in menus
        self.fzf = FzfPrompt()

    def findFolder(self):
        # Connect to the API service
        self.service = build("drive", "v3", credentials=self.creds)

        folderPicked = False
        parentFolder = self.parent_folder  # start with master folder from secrets.json
        curName = "TOP LEVEL FOLDER"
        while not folderPicked:
            results = (
                self.service.files()
                .list(
                    q=f"'{parentFolder}' in parents and mimeType = 'application/vnd.google-apps.folder'",
                    spaces="drive",
                    fields="files(id, name)",
                )
                .execute()
            )
            items = results.get("files", [])
            names_to_ids = dict()
            for item in items:
                names_to_ids[item.get("name")] = item.get("id")

            if len(items) == 0:
                # no subfolders! Potentially done, prompt user
                donemsg = f"No subfolders detected! Splice '{curName}?'"
                # for reasons unknown this returns the opposite of what the user clicked
                done = wt.Whiptail(title="Confirm Splice", height=20, width=60).yesno(
                    donemsg, default="no"
                )
                if not done:
                    folderPicked = True
                else:
                    # TODO: restart seach process
                    exit(0)

            else:
                # print a list of files
                selection_name = self.fzf.prompt(names_to_ids.keys(), FZF_ARGS)[0]
                parentFolder = names_to_ids.get(selection_name)
                curName = selection_name

        return parentFolder, curName

    def downloadFilm(self, folder_id):

        # remove existing files
        for file in os.scandir("staging"):
            os.remove(file.path)

        self.service = build("drive", "v3", credentials=self.creds)
        results = (
            self.service.files()
            .list(
                q=f"'{folder_id}' in parents", spaces="drive", fields="files(id, name)"
            )
            .execute()
        )
        files = results.get("files", [])

        for file in files:
            print(f"Downloading {file.get('name')}...")
            file_id = file.get("id")
            dl_path = f"staging/{file.get('name')}"
            print("begin")
            request = self.service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while done is False:
                status, done = downloader.next_chunk()
                print("Download %d%%." % int(status.progress() * 100))

            fh.seek(0)
            # Write the received data to the file
            with open(dl_path, "wb") as f:
                shutil.copyfileobj(fh, f)

    def spliceFilm(self):
        # check if there is enough space on disk
        merged_size = sum([file.stat().st_size for file in os.scandir('staging')])
        available_space = shutil.disk_usage('staging').free * DISK_USAGE_THRESHOLD
        if merged_size > available_space:
            print("Cannot splice! Not enough space available on disk")
            sys.exit(1)
        # Splice film together, store in staging/__merged.MP4
        subprocess.run(['sh', 'splice.sh'])

    def get_clips(self):
        clips = []
        for file in os.scandir("staging"):
            if file.name[-3:] == "MP4" and file.name != "__merged.MP4":
                clips.append(file.name)
        clips.sort()
        print(clips)
        return clips

    def format_desc(self):
        print("Generating chapters...")
        # get names and durations
        clipNames = self.get_clips()
        durations = []
        for clipName in clipNames:
            vid = VideoFileClip(f"staging/{clipName}")
            durations.append(vid.duration)
            del vid

        # generate formatted string
        desc = "Filmspliced! Clips:\n\n"
        curTime = 0
        for name, dur in zip(clipNames, durations):
            if name == "__merged.MP4":
                # skip merged if it exists
                continue
            # format time for use in YouTube auto-chapter generation
            timeStr = time.strftime("%H:%M:%S", time.gmtime(curTime))
            clipString = f"{timeStr} {name}\n"
            desc += clipString
            # increment time and do again
            curTime += dur
        print("Chapters generated")
        return desc

    def initialize_upload(self, name="tempTitle", playlist=None, chapters=True):
        file = "staging/__merged.MP4"
        if not os.path.exists(file):
            raise Exception("Missing __merged.MP4 in staging/")

        # format description
        if chapters:
            desc = self.format_desc()
        else:
            desc = "Filmspliced!"

        body = dict(
            snippet=dict(title=name, description=desc, tags=None),
            status=dict(privacyStatus="unlisted", selfDeclaredMadeForKids=False),
        )

        # Connect to the API service
        self.service = build("youtube", "v3", credentials=self.creds)
        # Call the API's videos.insert method to create and upload the video.
        insert_request = self.service.videos().insert(
            part=",".join(body.keys()),
            body=body,
            media_body=MediaFileUpload(file, chunksize=-1, resumable=True),
        )

        self.resumable_upload(insert_request, name, playlist)

    # FROM YOUTUBE API DOCUMENTATION
    # This method implements an exponential backoff strategy to resume a failed upload.
    def resumable_upload(self, insert_request, name, playlist):
        response = None
        error = None
        retry = 0
        while response is None:
            try:
                print("Uploading file...")
                status, response = insert_request.next_chunk()
                if response is not None:
                    if "id" in response:
                        print(f"Video id '{response['id']}' was successfully uploaded.")
                    else:
                        exit(
                            "The upload failed with an unexpected response: %s"
                            % response
                        )
            except HttpError as e:
                if e.resp.status in RETRIABLE_STATUS_CODES:
                    error = "A retriable HTTP error %d occurred:\n%s" % (
                        e.resp.status,
                        e.content,
                    )
                else:
                    raise
            except RETRIABLE_EXCEPTIONS as e:
                error = "A retriable error occurred: %s" % e

            if error is not None:
                print(error)
                retry += 1
            if retry > MAX_RETRIES:
                exit("No longer attempting to retry.")

            if not response:
                # sleep and retry if unsuccessful
                max_sleep = 2**retry
                sleep_seconds = random.random() * max_sleep
                print(f"Sleeping {sleep_seconds} seconds and then retrying...")
                time.sleep(sleep_seconds)

        # On successful upload, response will contain an id
        if "id" in response:
            # add to designated playlist if specified
            if playlist:
                self.add_to_playlist(response["id"], playlist)
            # sleep till processed and notify slack
            self.sleep_till_processed(response["id"], name)

    def sleep_till_processed(self, vid_id, name):
        max_attempts = 4
        # Call the API's videos.insert method to create and upload the video.
        request = self.service.videos().list(part="processingDetails", id=str(vid_id))
        status = None
        print("Checking processing status...")
        for attempt in range(max_attempts):
            time.sleep(5 * 60)  # sleep 5 mins
            response = request.execute()
            status = response["items"][0]["processingDetails"]["processingStatus"]
            if status == "processing":
                print("Still processing...")
            elif status == "succeeded":
                print("Done! Sending message to slack")
                self.send_success_message(vid_id, name)
                break

    def send_success_message(self, vid_id, name):
        message = f"Video '{name}' has been filmspliced! Don't leave me hanging, dap up www.youtube.com/watch?v={vid_id}"
        payload = {"text": message}
        x = requests.post(self.slack_hook, json=payload)

    def prompt_name(self, folderName):
        res = wt.Whiptail(title="Name Video", height=20, width=60).inputbox(
            msg="Enter video title", default=folderName
        )
        # if cancelled; exit
        if res[1]:
            sys.exit(0)
        return res[0]

    def prompt_playlist(self, name):
        # Get list of all channel playlists
        self.service = build("youtube", "v3", credentials=self.creds)
        playlists_request = self.service.playlists().list(
            part="snippet",
            mine=True,
            maxResults=25,
            fields="items/id,items/snippet/title",
        )
        response = playlists_request.execute()
        playlists = dict()
        for item in response.get("items"):
            playlists[item["snippet"]["title"]] = item["id"]

        menu_items = list(playlists.keys())
        choice, response = wt.Whiptail(
            title="Choose Playlist", height=20, width=60
        ).menu(msg=f"Choose a playlist for '{name}'", items=menu_items)
        if response:
            return None
        else:
            return playlists.get(choice)

    def add_to_playlist(self, video_id, playlist_id):
        playlistItem = dict(
            snippet=dict(
                playlistId=playlist_id,
                resourceId=dict(
                    kind="youtube#video",
                    videoId=video_id,
                ),
            )
        )

        # Connect to the API service
        self.service = build("youtube", "v3", credentials=self.creds)
        add_request = self.service.playlistItems().insert(
            part=",".join(playlistItem.keys()), body=playlistItem
        )
        response = add_request.execute()
        return response


def main():
    # make dl directory if necessary
    if not os.path.exists("staging"):
        os.mkdir("staging")

    toSplice, folderName = obj.findFolder()
    name = obj.prompt_name(folderName)
    playlist = obj.prompt_playlist(name)
    obj.downloadFilm(toSplice)
    obj.spliceFilm()
    obj.initialize_upload(name=name,playlist=playlist)


if __name__ == "__main__":
    try:
        obj = DriveAPI()
    except RefreshError:
        # if token expired; remove it and retry
        os.remove("token.pickle")
        obj = DriveAPI()

    existingFiles = [file.name for file in os.scandir("staging")]
    options = ["new splice", "resume splice", "retry upload"]
    user_selection = obj.fzf.prompt(options)[0]
    if user_selection == "new splice":
        # new splice
        main()
    elif user_selection == "resume splice":
        # resume splice
        if len(existingFiles) == 0:
            print("No files found in staging! Cannot resume splice")
            exit()
        obj.spliceFilm()
        obj.initialize_upload()
    elif user_selection == "retry upload":
        # resume upload
        if "__merged.MP4" not in existingFiles:
            print("Missing '__merged.MP4' in staging! Cannot resume upload")
            exit()
        obj.initialize_upload()
