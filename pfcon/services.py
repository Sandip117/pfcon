"""
This module implements the communication (API clients) with the coordinated pfioh and
pman services. Because communication with pman is not based on standard http we need
a pycurl-based hack here. Future pman's implementation should remove the need for pycurl.
"""

import logging
import io
import os
import json
import urllib.parse
from abc import ABC
import pycurl

from flask import g, current_app as app
from werkzeug.utils import secure_filename


logger = logging.getLogger(__name__)


class ServiceException(Exception):
    pass


class Service(ABC):
    """
    Abstract base class for the coordinated services.
    """
    NAME = 'abstract'

    def __init__(self, base_url):
        super().__init__()

        # base url of the service
        self.base_url = base_url

        # hardcode mounting points for the input and outputdir in the app's container!
        self.str_app_container_inputdir = '/share/incoming'
        self.str_app_container_outputdir = '/share/outgoing'

    def perform_request(self, curl_obj):
        try:
            curl_obj.perform()
        except pycurl.error as e:
            error_msg = f'Error in talking to {self.NAME} service, detail: {str(e)}'
            logger.error(error_msg)
            raise ServiceException(error_msg)
        finally:
            curl_obj.close()


class PmanService(Service):
    """
    Class for the pman service.
    """
    NAME = 'pman'

    def run_job(self, job_id, compute_data, data_share_dir):
        """
        Run process job on the compute environment ('run' action on pman).
        """
        payload = {
            'action': 'run',
            'meta': {
                    'cmd': self.build_app_cmd(compute_data),
                    'threaded': True,
                    'auid': compute_data['auid'],
                    'jid': job_id,
                    'number_of_workers': compute_data['number_of_workers'],
                    'cpu_limit': compute_data['cpu_limit'],
                    'memory_limit': compute_data['memory_limit'],
                    'gpu_limit': compute_data['gpu_limit'],
                    'container':
                        {
                            'target':
                                {
                                    'image': compute_data['image'],
                                    'cmdParse': False,
                                    'selfexec': compute_data['selfexec'],
                                    'selfpath': compute_data['selfpath'],
                                    'execshell': compute_data['execshell']
                                },
                            'manager':
                                {
                                    'image': 'fnndsc/swarm',
                                    'app': "swarm.py",
                                    'env':
                                        {
                                            'meta-store': 'key',
                                            'serviceType': 'docker',
                                            'shareDir': '%shareDir',
                                            'serviceName': job_id
                                        }
                                }
                        }
                }
        }
        payload['meta']['container']['manager']['env']['shareDir'] = data_share_dir
        return self.do_POST(payload)

    def get_job(self, job_id):
        """
        Get job info from the compute environment ('status' action on pman).
        """
        payload = {
            'action': 'status',
            'meta': {
                'key': 'jid',
                'value': job_id
            }
        }
        return self.do_POST(payload)

    def delete_job(self, job_id):
        pass

    def do_POST(self, payload):
        logger.info(f'Sending cmd to {self.NAME} service at -->{self.base_url}<--')
        logger.info('Payload sent: %s', json.dumps(payload, indent=4))

        c = pycurl.Curl()
        c.setopt(pycurl.CONNECTTIMEOUT, 1000)
        c.setopt(c.URL, self.base_url)
        buffer = io.BytesIO()
        c.setopt(pycurl.WRITEFUNCTION, buffer.write)
        post_data = json.dumps({'payload': payload})
        # form data must be provided already urlencoded:
        # post_data = urlencode({'payload': json.dumps(d_msg)})
        # but pman (wrongly) does not comply with this.
        # the next sets request method to POST,
        # Content-Type header to application/x-www-form-urlencoded
        # and data to send in request body
        c.setopt(c.POSTFIELDS, post_data)
        self.perform_request(c)
        str_resp = buffer.getvalue().decode()
        d_resp = json.loads(str_resp)
        logger.info(f'Response from {self.NAME}: {json.dumps(d_resp, indent=4)}')
        return d_resp

    def build_app_cmd(self, compute_data):
        """
        Build and return the app's cmd string.
        """
        cmd_args = compute_data['cmd_args']
        cmd_path_flags = compute_data['cmd_path_flags']
        if cmd_path_flags:
            # process the argument of any cmd flag that is a 'path'
            path_flags = cmd_path_flags.split(',')
            args = cmd_args.split()
            for i in range(len(args) - 1):
                if args[i] in path_flags:
                    # each flag value is a string of one or more paths separated by comma
                    # paths = args[i+1].split(',')
                    # base_inputdir = self.str_app_container_inputdir
                    # paths = [os.path.join(base_inputdir, p.lstrip('/')) for p in paths]
                    # args[i+1] = ','.join(paths)

                    # the next is tmp until CUBE's assumptions about inputdir and path
                    # parameters are removed
                    args[i+1] = self.str_app_container_inputdir
            cmd_args = ' '.join(args)
        selfpath = compute_data['selfpath']
        selfexec = compute_data['selfexec']
        execshell = compute_data['execshell']
        type = compute_data['type']
        outputdir = self.str_app_container_outputdir
        exec = os.path.join(selfpath, selfexec)
        cmd = f'{execshell} {exec}'
        if type == 'ds':
            inputdir = self.str_app_container_inputdir
            cmd = cmd + f' {inputdir} {outputdir} {cmd_args}'
        elif type in ('fs', 'ts'):
            cmd = cmd + f' {outputdir} {cmd_args}'
        else:
            ServiceException(f'Unsupported plugin type: {type}')
        return cmd

    @classmethod
    def get_service_obj(cls):
        if 'pman' not in g:
            g.pman = cls(app.config.get('COMPUTE_SERVICE_URL'))
        return g.pman


class PfiohService(Service):
    """
    Class for the pfioh service.
    """
    NAME = 'pfioh'

    def push_data(self, job_id, file_obj):
        """
        Push zip data file to pfioh ('pushPath' action on pfioh).
        """
        fname = secure_filename(file_obj.filename)
        payload = {
            'action': 'pushPath',
            'meta': {
                'remote': {'key': job_id},
                'local': {'path': fname},  # deprecated field
                'specialHandling': {
                    'op': 'plugin',
                    'cleanup': True
                },
                'transport': {
                    'mechanism': 'compress',
                    'compress': {
                        'archive':  'zip',
                        'unpack': True,
                        'cleanup':  True
                    }
                }
            }
        }
        logger.info(f'Sending PUSH data request to {self.NAME} at -->{self.base_url}<--')
        logger.info('Payload sent: %s', json.dumps(payload, indent=4))

        c = pycurl.Curl()
        c.setopt(pycurl.CONNECTTIMEOUT, 1000)
        c.setopt(c.URL, self.base_url)
        c.setopt(pycurl.HTTPHEADER, ['Mode: file'])
        c.setopt(
            pycurl.HTTPPOST,
            [
                ('d_msg',       json.dumps(payload)),
                ('filename',    fname),
                ('local',       (c.FORM_BUFFER, fname,
                                 c.FORM_BUFFERPTR, file_obj.read(),)
                 )
            ]
        )
        buffer = io.BytesIO()
        c.setopt(pycurl.WRITEFUNCTION, buffer.write)
        self.perform_request(c)
        str_resp = buffer.getvalue().decode()
        d_resp = json.loads(str_resp)
        logger.info(f'Response from {self.NAME}: {json.dumps(d_resp, indent=4)}')
        return d_resp

    def pull_data(self, job_id):
        """
        Pull zip data file from pfioh ('pullPath' action on pfioh).
        """
        d_query = {
            'action': 'pullPath',
            'meta': {
                'remote': {'key': job_id},
                'local': {
                    'path': job_id,  # deprecated field
                    'createDir': True
                },
                'specialHandling': {
                    'op': "plugin",
                    'cleanup': True
                },
                'transport': {
                    'mechanism': 'compress',
                    'compress': {
                        'archive':  'zip',
                        'unpack': True,
                        'cleanup':  True
                    }
                }
            }
        }
        query = urllib.parse.urlencode(d_query)
        logger.info(f'Sending PULL data request to {self.NAME} at -->{self.base_url}<--')
        logger.info('Query sent: %s', query)

        c = pycurl.Curl()
        c.setopt(pycurl.CONNECTTIMEOUT, 1000)
        c.setopt(c.URL, self.base_url + '?' + query)
        buffer = io.BytesIO()
        c.setopt(c.WRITEDATA, buffer)  # write bytes that are utf-8 encoded
        self.perform_request(c)  # perform a file transfer in this case
        content = buffer.getvalue()
        return content

    @classmethod
    def get_service_obj(cls):
        if 'pfioh' not in g:
            g.pfioh = cls(app.config.get('DATA_SERVICE_URL'))
        return g.pfioh