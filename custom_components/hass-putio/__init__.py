"""Support for Putio"""
from __future__ import annotations

import asyncio
from operator import index
from urllib.request import Request
import voluptuous as vol
import json
import os
import putiopy
import re

import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import config_entry_flow
from homeassistant.components import webhook
from homeassistant.const import CONF_DOMAIN, CONF_TOKEN, CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.components.downloader import (
    DOMAIN as DOWNLOADER,
    CONF_DOWNLOAD_DIR,
    SERVICE_DOWNLOAD_FILE,
    DOWNLOAD_COMPLETED_EVENT,
)

from .const import (
    LOGGER,
    DOMAIN,
    CONF_FILE_TYPES,
    CONF_MONITOR_FOLDERS,
    CONF_RETRY_ATTEMPTS,
    TRANSFER_COMPLETED_ID,
)

DEPENDENCIES = ["webhook"]

from zipfile import ZipFile

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_TOKEN): cv.string,
                vol.Optional(CONF_FILE_TYPES, default=[""]): cv.ensure_list_csv,
                vol.Optional(CONF_MONITOR_FOLDERS, default=[""]): cv.ensure_list_csv,
                vol.Optional(CONF_RETRY_ATTEMPTS, default=5): cv.positive_int,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigEntry) -> bool:
    """Set up the Putio service component."""
    if DOMAIN not in config:
        return True

    hass.data[DOMAIN] = config[DOMAIN]
    hass.data[DOMAIN][CONF_TOKEN] = config[DOMAIN][CONF_TOKEN]
    hass.data[DOMAIN][CONF_DOWNLOAD_DIR] = config[DOWNLOADER][CONF_DOWNLOAD_DIR]
    hass.data[DOMAIN][CONF_MONITOR_FOLDERS] = config[DOMAIN][CONF_MONITOR_FOLDERS]

    hass.components.webhook.async_register(
        DOMAIN,
        "putio",
        TRANSFER_COMPLETED_ID,
        handle_webhook,
    )

    def handle_event(event):
        LOGGER.debug("putio finished %s", event.data.get("filename"))
        download_dir = config[DOWNLOADER][CONF_DOWNLOAD_DIR]
        meta_file_path = "{}InProgress/{}_meta.json".format(
            download_dir, event.data.get("filename").replace(".zip", "")
        )
        with open(meta_file_path, "r", encoding="UTF-8") as read_file:
            zip_metadata = json.load(read_file)
        zip_file_path = "{}InProgress/{}".format(
            download_dir, event.data.get("filename")
        )
        with ZipFile(zip_file_path, "r") as zip_file:
            for member in zip_file.infolist():
                filename = os.path.basename(member.filename)
                if filename and (
                    not config[DOMAIN][CONF_FILE_TYPES]
                    or filename.endswith(tuple(config[DOMAIN][CONF_FILE_TYPES]))
                ):
                    LOGGER.debug("extracting %s", filename)
                    member.filename = filename
                    tv_show_name = ""
                    if zip_metadata["sub_folder"] == "TV":
                        tv_name_regex = re.compile(
                            r"(?P<showname>[ \.\w]*)s(\d*)e(\d*)", re.IGNORECASE
                        )
                        name_match = tv_name_regex.search(filename)
                        tv_show_name = (
                            name_match.group("showname").replace(".", " ").strip(" ")
                        )
                        tv_show_name = "{}/".format(tv_show_name)
                    zip_file.extract(
                        member,
                        "{}{}/{}".format(
                            download_dir, zip_metadata["sub_folder"], tv_show_name
                        ),
                    )
                    hass.components.persistent_notification.create(
                        "{} has been downloaded".format(filename), title="Put.io"
                    )
            os.remove(zip_file_path)
            os.remove(meta_file_path)

    hass.bus.async_listen(
        "{}_{}".format(DOWNLOADER, DOWNLOAD_COMPLETED_EVENT), handle_event
    )

    return True


async def handle_webhook(hass, webhook_id, request):
    data = dict(await request.post())

    if not data["file_id"]:
        LOGGER.warning(
            "Put.io webhook received an unrecognized payload - content:%s", data
        )
        return

    hass.async_create_task(handle_file(hass, hass.data[DOMAIN][CONF_TOKEN], data))
    return


async def handle_file(hass, token, data):
    client = putiopy.Client(token)
    file_id = data["file_id"]

    LOGGER.debug("new file %s", data)

    sub_folder_name = await get_sub_folder(hass, client, file_id)
    if sub_folder_name not in tuple(hass.data[DOMAIN][CONF_MONITOR_FOLDERS]):
        LOGGER.debug("file not in monitored folder: %s", sub_folder_name)
        return

    zip_id = await create_zip_file(hass, client, file_id)
    zip_download_link = await get_zip_download_link(hass, client, zip_id)

    create_file_meta(hass, data, zip_id, zip_download_link, sub_folder_name)
    download_file(hass, zip_download_link, zip_id, "InProgress")
    return


async def get_sub_folder(hass, client: putiopy.Client, file_id):
    loop = asyncio.get_event_loop()

    file_future = loop.run_in_executor(None, lambda: client.File.get(id=int(file_id)))
    file = await file_future

    movies_future = loop.run_in_executor(None, lambda: client.File.search("Movies"))
    movies_folder = await movies_future

    tv_future = loop.run_in_executor(None, lambda: client.File.search("TV"))
    tv_folder = await tv_future

    file_parent_id = file.parent_id

    if file_parent_id == movies_folder[0].id:
        sub_folder = "Movies"
    elif file_parent_id == tv_folder[0].id:
        sub_folder = "TV"
    else:
        sub_folder = "Other"

    return sub_folder


async def create_zip_file(hass, client: putiopy.Client, file_id):
    LOGGER.debug("zipping: %s", file_id)
    path = "/zips/create"
    data = {"file_ids": file_id}

    loop = asyncio.get_event_loop()

    future = loop.run_in_executor(None, lambda: client.request(path, "POST", data=data))
    response = await future

    return response["zip_id"]


async def get_zip_download_link(hass, client: putiopy.Client, zip_id):
    LOGGER.debug("getting zip: %s", zip_id)
    path = "/zips/%s" % zip_id

    loop = asyncio.get_event_loop()

    for i in range(1, hass.data[DOMAIN][CONF_RETRY_ATTEMPTS]):
        LOGGER.debug("Attempt: %d", i)

        await asyncio.sleep(15 * i)

        future_zip = loop.run_in_executor(None, lambda: client.request(path, "GET"))
        zip_item = await future_zip

        query_status = zip_item["status"].lower()
        zip_status = zip_item["zip_status"].lower()

        if query_status == "ok" and zip_status == "done":
            return zip_item["url"]


def create_file_meta(hass, data, zip_id, zip_download_link, sub_folder):
    download_dir = hass.data[DOMAIN][CONF_DOWNLOAD_DIR]
    meta_file = "{}/InProgress/{}_meta.json".format(download_dir, zip_id)
    meta_data = {"zip_id": zip_id, "zip_download_link": zip_download_link, "file_id": data["file_id"], "sub_folder": sub_folder}

    with open(meta_file, "w", encoding="UTF-8") as outfile:
        json.dump(meta_data, outfile)


def download_file(hass, url, filename, subfolder):
    data = {
        "url": url,
        "filename": "{}.zip".format(filename),
        "overwrite": "true",
        "subdir": subfolder,
    }
    asyncio.run_coroutine_threadsafe(
        hass.services.async_call(
            DOWNLOADER, SERVICE_DOWNLOAD_FILE, data, blocking=True
        ),
        hass.loop,
    )
