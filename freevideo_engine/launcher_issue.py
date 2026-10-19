"""The launcher's "Report a problem" page: the installation/launcher form with the failure already in it.

Only the sentence the failure card shows travels. It has gone through the launcher's redaction already;
paths and long tokens are removed once more here, and the end of a long log gives way to fit the address.
"""
from urllib.parse import urlencode

from .feedback import scrub

ISSUE_FORM = 'https://github.com/FlashML-org/FreeVideo/issues/new'
# GitHub refuses very long addresses.
MAX_ADDRESS = 7000
MAX_PROBLEM = 600
MAX_REPORT = 2500


def _lines(error):
    return [line for line in scrub(error).splitlines() if line.strip()]


def issue_url(error, version=None, title=None, zh=False):
    lines = _lines(error)
    first = (lines[-1] if lines and lines[0].startswith('Traceback') else (lines[0] if lines else '')).strip()
    place = '启动器' if zh else 'Launcher'
    problem = ('FreeVideo 启动器显示了一个错误。\n' if zh else 'The FreeVideo launcher showed an error.\n') + first[:300]
    report = '\n'.join(lines)

    def fit(limit):
        return report if len(report) <= limit else report[:limit // 3] + '\n...\n' + report[-(limit - limit // 3 - 5):]

    def build(body):
        params = {'template': 'bug_launcher.yml', 'title': f'[{place}] ' + (str(title).strip() if title else place),
                  'problem': problem[:MAX_PROBLEM], 'report': body}
        if version:
            params['version'] = 'v' + str(version).lstrip('v')
        return ISSUE_FORM + '?' + urlencode(params)

    limit = MAX_REPORT
    while len(build(fit(limit))) > MAX_ADDRESS and limit > 50:
        limit = int(limit * 0.8)
    return build(fit(limit))
