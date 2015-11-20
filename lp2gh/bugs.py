import re
import urllib2
import time
import json

import gflags
import jsontemplate

from lp2gh import client
from lp2gh import exporter
from lp2gh import labels
from lp2gh import util


FLAGS = gflags.FLAGS
gflags.DEFINE_boolean('only_open_bugs', False,
                      'should we include closed bugs')


BUG_STATUS = ['New',
              'Incomplete',
              'Invalid',
              "Won't Fix",
              'Confirmed',
              'Triaged',
              'In Progress',
              'Fix Committed',
              'Fix Released']


BUG_CLOSED_STATUS = ['Invalid',
                     "Won't Fix",
                     'Fix Released']


BUG_IMPORTANCE = ['Critical',
                  'High',
                  'Medium',
                  'Low',
                  'Wishlist',
                  'Undecided']


MAX_RETRIES = 30
def limit_retry(e, repo, try_block, catch_block=None, give_up_block=None):
  retries = 0
  while True:
    try:
      return try_block()
    except urllib2.HTTPError as err:
      if catch_block:
        catch_block(err)
      else:
        e.emit('exception: %s' % err.read())
      if retries >= MAX_RETRIES:
        if give_up_block: give_up_block(err)
        break
      else:
        retries = retries + 1
        resp = repo.client.get("https://api.github.com/rate_limit")
        d = json.loads(resp.read())
        limits = (d["resources"]["core"]["remaining"], d["resources"]["core"]["reset"])
        e.emit('current rate limits: %s' % str(limits))
        if limits[0] > 0:
          e.emit('sleeping quietly for %d minutes ...' % retries)
          time.sleep(60 * retries)
        else:
          offset = limits[1] - int(time.time()) + 10
          e.emit('rate limit reached - have to sleep for %d seconds' % offset)
          time.sleep(offset)

bug_matcher_re = re.compile(r'bug (\d+)')


BUG_SUMMARY_TEMPLATE = """
------------------------------------
Imported from Launchpad using lp2gh.

 * date created: {date_created}{.section owner}
 * owner: {owner}{.end}{.section assignee}
 * assignee: {assignee}{.end}{.section duplicate_of}
 * duplicate of: #{duplicate_of}{.end}{.section duplicates}
 * the following issues have been marked as duplicates of this one:{.repeated section @}
   * #{@}{.end}{.end}
 * the launchpad url was {lp_url}
"""


def message_to_dict(message):
  owner = message.owner
  return {'owner': owner.name,
          'content': message.content,
          'date_created': util.to_timestamp(message.date_created),
          }


def bug_task_to_dict(bug_task):
  bug = bug_task.bug
  assignee = bug_task.assignee
  owner = bug_task.owner
  messages = list(bug.messages)[1:]
  milestone = bug_task.milestone
  duplicates = bug.duplicates
  duplicate_of = bug.duplicate_of
  return {'id': bug.id,
          'status': bug_task.status,
          'importance': bug_task.importance,
          'assignee': assignee and assignee.name or None,
          'owner': owner.name,
          'milestone': milestone and milestone.name,
          'title': bug.title,
          'description': bug.description,
          'duplicate_of': duplicate_of and duplicate_of.id or None,
          'duplicates': [x.id for x in duplicates],
          'date_created': util.to_timestamp(bug_task.date_created),
          'comments': [message_to_dict(x) for x in messages],
          'tags': bug.tags,
          'security_related': bug.security_related,
          'lp_url': bug.web_link,
          }


def list_bugs(project, only_open=None):
  if only_open is None:
    only_open = FLAGS.only_open_bugs
  return project.searchTasks(status=only_open and None or BUG_STATUS)


def _replace_bugs(s, bug_mapping):
  matches = bug_matcher_re.findall(s)
  for match in matches:
    if match in bug_mapping:
      new_id = bug_mapping[match]
      s = s.replace('bug %s' % match, 'bug #%s' % new_id)
  return s


def translate_auto_links(bug, bug_mapping):
  """Update references to launchpad bug numbers to reference issues."""
  bug['description'] = _replace_bugs(bug['description'], bug_mapping)
  #bug['description'] = '```\n' + bug['description'] + '\n```'
  for comment in bug['comments']:
    comment['content'] = _replace_bugs(comment['content'], bug_mapping)
    #comment['content'] = '```\n' + comment['content'] + '\n```'

  return bug


def add_summary(bug, bug_mapping):
  """Add the summary information to the bug."""
  t = jsontemplate.FromString(BUG_SUMMARY_TEMPLATE)
  bug['duplicate_of'] = bug['duplicate_of'] in bug_mapping and bug_mapping[bug['duplicate_of']] or None
  bug['duplicates'] = [bug_mapping[x] for x in bug['duplicates']
                       if x in bug_mapping]
  bug['description'] = bug['description'] + '\n' + t.expand(bug)
  return bug


def export(project, only_open=None):
  o = []
  c = client.Client()
  p = c.project(project)
  e = exporter.Exporter()
  bugs = list_bugs(p, only_open=only_open)
  for x in bugs:
    e.emit('fetching %s' % x.title)
    rv = bug_task_to_dict(x)
    o.append(rv)
  return o


def import_(repo, bugs, milestones_map=None):
  e = exporter.Exporter()
  # set up all the labels we know
  for status in BUG_STATUS:
    try:
      e.emit('create label %s' % status)
      labels.create_label(repo, status, 'ddffdd')
    except Exception as err:
      e.emit('exception: %s' % err.read())

  for importance in BUG_IMPORTANCE:
    try:
      e.emit('create label %s' % importance)
      labels.create_label(repo, importance, 'ffdddd')
    except Exception as err:
      e.emit('exception: %s' % err.read())

  tags = []
  for x in bugs:
    tags.extend(x['tags'])
  tags = set(tags)

  # NOTE(termie): workaround for github case-sensitivity bug
  defaults_lower = [x.lower() for x in (BUG_STATUS + BUG_IMPORTANCE)]
  tags = [x for x in tags if str(x.lower()) not in defaults_lower]
  tags_map = dict((x.lower(), x) for x in (tags + BUG_STATUS + BUG_IMPORTANCE))

  for tag in tags:
    try:
      e.emit('create label %s' % tag)
      labels.create_label(repo, tag)
    except Exception as err:
      e.emit('exception: %s' % err.read())

  mapping = {}
  # first pass
  issues = repo.issues()
  for bug in bugs:
    e.emit('create issue %s' % bug['title'])
    params = {'title': bug['title'],
              'body': bug['description'],
              'labels': bug['tags'] + [bug['importance']] + [bug['status']],
              # NOTE(termie): github does not support setting created_at
              #'created_at': bug['date_created'],
              }

    # NOTE(termie): workaround for github case-sensitivity bug
    params['labels'] = list(set(
      [labels.translate_label(tags_map[x.lower()]) for x in params['labels']]))

    e.emit('with params: %s' % params)
    rv = limit_retry(e, repo, lambda: issues.append(**params))
    mapping[bug['id']] = rv['number']

  # second pass
  for bug in bugs:
    e.emit('second pass on issue %s' % bug['title'])
    bug = translate_auto_links(bug, mapping)
    bug = add_summary(bug, mapping)
    issue_id = mapping[bug['id']]
    issue = repo.issue(issue_id)

    # add all the comments
    comments = repo.comments(issue_id)
    for msg in bug['comments']:
      # TODO(termie): username mapping
      by_line = '(by %s)' % msg['owner']
      limit_retry(e, repo, lambda: comments.append(body='%s\n%s' % (by_line, msg['content'])))

    # update the issue
    params = {'body': bug['description']}
    if bug['status'] in BUG_CLOSED_STATUS:
      params['state'] = 'closed'

    # NOTE(termie): workaround a bug in github where it does not allow
    #               creating bugs that are assigned to double-digit milestones
    #               but does allow editing an existing bug
    if bug['milestone']:
      params['milestone'] = milestones_map[bug['milestone']]
    limit_retry(e, repo, lambda: issue.update(params))

  return mapping
