import datetime
import json
import math
import re
from itertools import groupby, zip_longest

import requests
from jira import JIRA, JIRAError


JIRA_KEY_PATTERN = re.compile(r"^(?P<key>[A-Z]{1,10}-\d+)\b")
JIRA_WORKLOG_ID_PATTERN = re.compile(r"^jira-worklog-(?P<id>\d+)$")

ROUND_SECONDS_TO = 30 * 60


try:
    with open("config.json", "r") as f:
        config = json.load(f)
except (OSError, ValueError):
    config = {}


toggl_api_token = config.get("toggl_api_token") or input(
    "Toggl API token (see https://toggl.com/app/profile#reset_api_token): "
)
if not toggl_api_token:
    exit("Toggl api token required to continue")


jira_server = config.get("jira_server") or input(
    "JIRA server (e.g. https://webitau.atlassian.net): "
)
if not jira_server:
    exit("JIRA server required to continue")


jira_email = config.get("jira_email") or input("JIRA email: ")
if not jira_email:
    exit("JIRA email required to continue")


jira_api_token = config.get("jira_api_token") or input(
    "JIRA API token (create at https://id.atlassian.com/manage/api-tokens): "
)
if not jira_api_token:
    exit("JIRA password required to continue")


with open("config.json", "w") as f:
    json.dump(
        {
            "jira_email": jira_email,
            "jira_server": jira_server,
            "jira_api_token": jira_api_token,
            "toggl_api_token": toggl_api_token,
        },
        f,
        indent=4,
    )


del config


jira = JIRA(jira_server, basic_auth=(jira_email, jira_api_token), max_retries=0)
jira_account_id = jira.myself()["accountId"]

toggl = requests.Session()
toggl.auth = (toggl_api_token, "api_token")
toggl.params["user_agent"] = jira_email


def load_timestamp(s):
    try:
        ts = datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        ts = datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")
    return ts.replace(tzinfo=datetime.timezone.utc) + ts.utcoffset()


jira_key_time_entries = {}

for time_entry in toggl.get("https://www.toggl.com/api/v8/time_entries").json():
    # see if its for a JIRA ticket
    match = JIRA_KEY_PATTERN.match(time_entry["description"])
    if not match:
        continue

    jira_key = match.group("key")
    jira_key_time_entries.setdefault(jira_key, []).append(time_entry)


for jira_key, time_entries in jira_key_time_entries.items():
    jira_worklogs = [w for w in jira.worklogs(jira_key) if w.author.accountId == jira_account_id]

    # check if enough hours are already logged
    jira_worklog_total = sum(w.timeSpentSeconds for w in jira_worklogs)
    time_entry_total = sum(e["duration"] for e in time_entries)
    time_entry_total = math.ceil(time_entry_total / ROUND_SECONDS_TO) * ROUND_SECONDS_TO
    time_shortfall = max(time_entry_total - jira_worklog_total, 0)
    if not time_shortfall:
        continue

    print("")
    print(time_entries[0]["description"])
    print("total", time_entry_total)
    print("jira", jira_worklog_total)
    print("shortfall", time_shortfall)

    # normalise started timestamps and sort
    for time_entry in time_entries:
        time_entry["start"] = load_timestamp(time_entry["start"])
    for jira_worklog in jira_worklogs:
        jira_worklog.started = load_timestamp(jira_worklog.started)
    jira_worklogs.sort(key=lambda w: w.started)
    time_entries.sort(key=lambda e: e["start"])

    # go through the worklogs, and whenever it's behind the total for entries, update it to match
    # whatever's left, add to the last worklog (if any) or a new work_log for the last entry date

    time_entry_total = 0
    jira_worklog_total = 0

    for time_entry, jira_worklog in zip_longest(time_entries, jira_worklogs):
        if time_entry:
            time_entry_total += time_entry["duration"]
        if jira_worklog:
            jira_worklog_total += jira_worklog.timeSpentSeconds

        if jira_worklog is not None and jira_worklog_total < time_entry_total:
            shortfall = time_entry_total - jira_worklog_total
            shortfall = math.ceil(shortfall / ROUND_SECONDS_TO) * ROUND_SECONDS_TO
            print(
                "Updating %s worklog %s to %.1g hrs" % jira_key,
                jira_worklog.started,
                (jira_worklog.timeSpentSeconds + shortfall) / 3600,
            )
            jira_worklog.update(timeSpentSeconds=jira_worklog.timeSpentSeconds + shortfall)
            jira_worklog_total += shortfall

    if jira_worklog_total < time_entry_total:
        shortfall = time_entry_total - jira_worklog_total
        shortfall = math.ceil(shortfall / ROUND_SECONDS_TO) * ROUND_SECONDS_TO
        if jira_worklogs:
            jira_worklog = jira_worklogs[-1]
            print(
                "Updating %s worklog %s to %.1g hrs" % jira_key,
                jira_worklog.started.strftime("%c"),
                (jira_worklog.timeSpentSeconds + shortfall) / 3600,
            )
            jira_worklog.update(timeSpentSeconds=jira_worklog.timeSpentSeconds + shortfall)
        else:
            time_entry = time_entries[-1]
            print(
                "Creating %s worklog %s to %.1g hrs" % jira_key,
                time_entry["start"].strftime("%c"),
                (jira_worklog.timeSpentSeconds + shortfall) / 3600,
            )
            jira.add_worklog(jira_key, timeSpentSeconds=shortfall, started=time_entry["start"])
