# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
#
# Copyright 2016 Stefan Breunig <stefan-duplicity@breunig.xyz>
# Based on the backend onedrivebackend.py
#
# This file is part of duplicity.
#
# Duplicity is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation; either version 2 of the License, or (at your
# option) any later version.
#
# Duplicity is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with duplicity; if not, write to the Free Software Foundation,
# Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA

import os.path
import json
import sys
import re
from io import DEFAULT_BUFFER_SIZE

import duplicity.backend
from duplicity.errors import BackendException
from duplicity import globals
from duplicity import log

class AmazonDriveBackend(duplicity.backend.Backend):
    """
    Backend for AmazonDrive. It communicates directly with AmazonDrive using
    their RESTful API and does not rely on externally setup software (like
    acd_cli).
    """

    OAUTH_TOKEN_PATH = os.path.expanduser('~/.duplicity_amazondrive_oauthtoken.json')

    OAUTH_AUTHORIZE_URL = 'https://www.amazon.com/ap/oa'
    OAUTH_TOKEN_URL = 'https://api.amazon.com/auth/o2/token'
    OAUTH_REDIRECT_URL = 'http://127.0.0.1/'
    OAUTH_SCOPE = ['clouddrive:read_other', 'clouddrive:write']

    CLIENT_ID = 'amzn1.application-oa2-client.791c9c2d78444e85a32eb66f92eb6bcc'
    CLIENT_SECRET = '5b322c6a37b25f16d848a6a556eddcc30314fc46ae65c87068ff1bc4588d715b'

    MULTIPART_BOUNDARY = 'DuplicityFormBoundaryd66364f7f8924f7e9d478e19cf4b871d114a1e00262542'

    def __init__(self, parsed_url):
        duplicity.backend.Backend.__init__(self, parsed_url)

        self.metadata_url = 'https://drive.amazonaws.com/drive/v1/'
        self.content_url = 'https://content-na.drive.amazonaws.com/cdproxy/'

        self.names_to_ids = {}
        self.backup_target_id = None
        self.backup_target = parsed_url.path.lstrip('/')

        if globals.volsize > (10 * 1024 * 1024 * 1024):
            # https://forums.developer.amazon.com/questions/22713/file-size-limits.html
            # https://forums.developer.amazon.com/questions/22038/support-for-chunked-transfer-encoding.html
            log.FatalError(
                'Your --volsize is bigger than 10 GiB, which is the maximum '
                'file size on AmazonDrive that does not require work arounds.')

        try:
            global requests
            global OAuth2Session
            import requests
            from requests_oauthlib import OAuth2Session
        except ImportError:
            raise BackendException(
                'AmazonDrive backend requires python-requests and '
                'python-requests-oauthlib to be installed.\n\n'
                'For Debian and derivates use:\n'
                '  apt-get install python-requests python-requests-oauthlib\n'
                'For Fedora and derivates use:\n'
                '  yum install python-requests python-requests-oauthlib')

        self.initialize_oauth2_session()
        self.resolve_backup_target()

    def initialize_oauth2_session(self):
        """Setup or refresh oauth2 session with AmazonDrive"""

        def token_updater(token):
            """Stores oauth2 token on disk"""
            try:
                with open(self.OAUTH_TOKEN_PATH, 'w') as f:
                    json.dump(token, f)
            except Exception as err:
                log.Error('Could not save the OAuth2 token to %s. This means '
                          'you may need to do the OAuth2 authorization '
                          'process again soon. Original error: %s' % (
                              self.OAUTH_TOKEN_PATH, err))

        token = None
        try:
            with open(self.OAUTH_TOKEN_PATH) as f:
                token = json.load(f)
        except IOError as err:
            log.Notice('Could not load OAuth2 token. '
                       'Trying to create a new one. (original error: %s)' % err)

        self.http_client = OAuth2Session(
            self.CLIENT_ID,
            scope=self.OAUTH_SCOPE,
            redirect_uri=self.OAUTH_REDIRECT_URL,
            token=token,
            auto_refresh_kwargs={
                'client_id': self.CLIENT_ID,
                'client_secret': self.CLIENT_SECRET,
            },
            auto_refresh_url=self.OAUTH_TOKEN_URL,
            token_updater=token_updater)

        if token is not None:
            self.http_client.refresh_token(self.OAUTH_TOKEN_URL)

        endpoints_response = self.http_client.get(self.metadata_url
                                                  + 'account/endpoint')
        if endpoints_response.status_code != requests.codes.ok:
            token = None

        if token is None:
            if not sys.stdout.isatty() or not sys.stdin.isatty():
                log.FatalError('The OAuth2 token could not be loaded from %s '
                               'and you are not running duplicity '
                               'interactively, so duplicity cannot possibly '
                               'access AmazonDrive.' % self.OAUTH_TOKEN_PATH)
            authorization_url, _ = self.http_client.authorization_url(
                self.OAUTH_AUTHORIZE_URL)

            print ''
            print ('In order to authorize duplicity to access your '
                   'AmazonDrive, please open the following URL in a browser '
                   'and copy the URL the blank page the dialog leads to: '
                   '%s' % authorization_url)
            print ''

            redirected_to = (raw_input('URL of the blank page: ')
                             .replace('http://', 'https://', 1))

            token = self.http_client.fetch_token(
                self.OAUTH_TOKEN_URL,
                client_secret=self.CLIENT_SECRET,
                authorization_response=redirected_to)

            endpoints_response = self.http_client.get(self.metadata_url
                                                      + 'account/endpoint')
            endpoints_response.raise_for_status()
            token_updater(token)

        urls = endpoints_response.json()
        if 'metadataUrl' not in urls or 'contentUrl' not in urls:
            log.FatalError('Could not retrieve endpoint URLs for this account')
        self.metadata_url = urls['metadataUrl']
        self.content_url = urls['contentUrl']

    def resolve_backup_target(self):
        """Resolve node id for remote backup target folder"""

        response = self.http_client.get(
            self.metadata_url + 'nodes?filters=kind:FOLDER AND isRoot:true')
        parent_node_id = response.json()['data'][0]['id']

        for component in [x for x in self.backup_target.split('/') if x]:
            # There doesn't seem to be escaping support, so cut off filter
            # after first unsupported character
            filter = re.search('^[A-Za-z0-9_-]*', component).group(0)
            if component != filter:
                filter = filter + '*'

            matches = self.read_all_pages(
                self.metadata_url + 'nodes?filters=kind:FOLDER AND name:%s '
                                    'AND parents:%s' % (filter, parent_node_id))
            candidates = [f for f in matches if f.get('name') == component]

            if len(candidates) >= 2:
                log.FatalError('There are multiple folders with the same name '
                               'below one parent.\nParentID: %s\nFolderName: '
                               '%s' % (parent_node_id, component))
            elif len(candidates) == 1:
                parent_node_id = candidates[0]['id']
            else:
                log.Debug('Folder %s does not exist yet. Creating.' % component)
                parent_node_id = self.mkdir(parent_node_id, component)

        log.Debug("Backup target folder has id: %s" % parent_node_id)
        self.backup_target_id = parent_node_id

    def get_file_id(self, remote_filename):
        """Find id of remote file in backup target folder"""

        if remote_filename not in self.names_to_ids:
            self._list()

        return self.names_to_ids.get(remote_filename)

    def mkdir(self, parent_node_id, folder_name):
        """Create a new folder as a child of a parent node"""

        data = {'name': folder_name, 'parents': [parent_node_id], 'kind' : 'FOLDER'}
        response = self.http_client.post(
            self.metadata_url + 'nodes',
            data=json.dumps(data))
        response.raise_for_status()
        return response.json()['id']

    def multipart_stream(self, metadata, source_path):
        """Generator for multipart/form-data file upload from source file"""

        boundary = self.MULTIPART_BOUNDARY

        yield str.encode('--%s\r\nContent-Disposition: form-data; '
                         'name="metadata"\r\n\r\n' % boundary +
                         '%s\r\n' % json.dumps(metadata) +
                         '--%s\r\n' % boundary)
        yield b'Content-Disposition: form-data; name="content"; filename="i_love_backups"\r\n'
        yield b'Content-Type: application/octet-stream\r\n\r\n'

        with source_path.open() as stream:
            while True:
                f = stream.read(DEFAULT_BUFFER_SIZE)
                if f:
                    yield f
                else:
                    break

        yield str.encode('\r\n--%s--\r\n' % boundary +
                         'multipart/form-data; boundary=%s' % boundary)

    def read_all_pages(self, url):
        """Iterates over nodes API URL until all pages were read"""

        result = []
        next_token = ''
        token_param = '&startToken=' if '?' in url else '?startToken='

        while True:
            paginated_url = url + token_param + next_token
            json_response = self.http_client.get(paginated_url).json()

            result.extend(json_response['data'])

            # Amazon's documentation incorrectly states that the nextToken is
            # always present. The second condition is as per documentation:
            # https://developer.amazon.com/public/apis/experience/cloud-drive/content/nodes#Pagination
            if 'nextToken' not in json_response or len(json_response['data']) == 0:
                break

            # Do not make another HTTP request if everything is here already
            if len(result) >= json_response['count']:
                break

            next_token = json_response['nextToken']

        return result

    def _put(self, source_path, remote_filename):
        """Upload a local file to AmazonDrive"""

        quota = self.http_client.get(self.metadata_url + 'account/quota')
        quota.raise_for_status()
        available = quota.json()['available']

        source_size = os.path.getsize(source_path.name)

        if source_size > available:
            raise BackendException(
                'Out of space: trying to store "%s" (%d bytes), but only '
                '%d bytes available on AmazonDrive.' % (
                    source_path.name, source_size, available))

        # Just check the cached list, to avoid _list for every new file being
        # uploaded
        if remote_filename in self.names_to_ids:
            log.Debug('File %s seems to already exist on AmazonDrive. Deleting '
                      'before attempting to upload it again.' % remote_filename)
            self._delete(remote_filename)

        metadata = {'name': remote_filename, 'kind': 'FILE',
                    'parents': [self.backup_target_id]}
        headers = {'Content-Type': 'multipart/form-data; boundary=%s'
                                   % self.MULTIPART_BOUNDARY}
        data = self.multipart_stream(metadata, source_path)

        response = self.http_client.post(
            self.content_url + 'nodes?suppress=deduplication',
            data=data,
            headers=headers)

        if response.status_code == 409: # "409 : Duplicate file exists."
            self._delete(remote_filename)
            raise BackendException('Upload failed, because there was a file with '
                           'the same name as %s already present. The file was '
                           'deleted, and duplicity will retry the upload unless '
                           'the retry limit has been reached.' % remote_filename)
        else:
            response.raise_for_status()

        json = response.json()
        if 'id' not in json:
            raise BackendException('%s was uploaded, but returned JSON does not '
                           'contain ID of new file. Retrying.\nJSON:\n\n%s'
                           % (remote_filename, json))

        # XXX: The upload may be considered finished before the file shows up
        # in the file listing. As such, the following is required to avoid race
        # conditions when duplicity calls _query or _list.
        self.names_to_ids[response.json()['name']] = json['id']

    def _get(self, remote_filename, local_path):
        """Download file from AmazonDrive"""

        with local_path.open('wb') as local_file:
            file_id = self.get_file_id(remote_filename)
            if file_id is None:
                raise BackendException(
                    'File "%s" cannot be downloaded: it does not exist' %
                    remote_filename)

            response = self.http_client.get(
                self.content_url + '/nodes/' + file_id + '/content', stream=True)
            response.raise_for_status()
            for chunk in response.iter_content(chunk_size=DEFAULT_BUFFER_SIZE):
                if chunk:
                    local_file.write(chunk)
            local_file.flush()

    def _query(self, remote_filename):
        """Retrieve file size info from AmazonDrive"""

        file_id = self.get_file_id(remote_filename)
        if file_id is None:
            return {'size': -1}
        response = self.http_client.get(self.metadata_url + 'nodes/' + file_id)
        response.raise_for_status()

        return {'size': response.json()['contentProperties']['size']}

    def _list(self):
        """List files in AmazonDrive backup folder"""

        files = self.read_all_pages(
            self.metadata_url + 'nodes/' + self.backup_target_id +
            '/children?filters=kind:FILE')

        self.names_to_ids = {f['name']: f['id'] for f in files}

        return self.names_to_ids.keys()

    def _delete(self, remote_filename):
        """Delete file from AmazonDrive"""

        file_id = self.get_file_id(remote_filename)
        if file_id is None:
            raise BackendException(
                'File "%s" cannot be deleted: it does not exist' % (
                    remote_filename))
        response = self.http_client.put(self.metadata_url + 'trash/' + file_id)
        response.raise_for_status()
        del self.names_to_ids[remote_filename]

duplicity.backend.register_backend('amazondrive', AmazonDriveBackend)
