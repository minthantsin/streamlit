# -*- coding: future_fstrings -*-
# Copyright 2018 Streamlit Inc. All rights reserved.

"""Handles a connecton to an S3 bucket to send Report data."""

# Python 2/3 compatibility
from __future__ import print_function, division, unicode_literals, absolute_import
from streamlit.compatibility import setup_2_3_shims
setup_2_3_shims(globals())

import boto3
import botocore
import logging
import math
import mimetypes
import os

from tornado import gen
from tornado.concurrent import run_on_executor, futures

from streamlit import errors
from streamlit import config
from streamlit.proxy.storage.AbstractStorage import AbstractStorage

from streamlit.logger import get_logger
LOGGER = get_logger(__name__)


class S3Storage(AbstractStorage):
    """Class to handle S3 uploads."""

    executor = futures.ThreadPoolExecutor(5)

    def __init__(self):
        """Constructor."""
        super(S3Storage, self).__init__()

        # For now don't enable verbose boto logs
        # TODO(armando): Make this configurable.
        log = logging.getLogger('botocore')
        log.propagate = False

        # Config related stuff.
        self._bucketname = config.get_option('s3.bucket')
        self._url = config.get_option('s3.url')
        self._key_prefix = config.get_option('s3.keyPrefix')
        self._region = config.get_option('s3.region')

        user = os.getenv('USER', None)

        if self._url and '{USER}' in self._url:
            self._url = self._url.replace('{USER}', user)
        if self._key_prefix and '{USER}' in self._key_prefix:
            self._key_prefix = self._key_prefix.replace('{USER}', user)

        # URL where browsers go to load the Streamlit web app.
        self._web_app_url = None

        if not self._url:
            self._web_app_url = os.path.join(
                'https://%s.%s' % (self._bucketname, 's3.amazonaws.com'),
                self._s3_key('index.html'))
        else:
            self._web_app_url = os.path.join(
                self._url,
                self._s3_key('index.html', add_prefix=False))

        aws_profile = config.get_option('s3.profile')
        access_key_id = config.get_option('s3.accessKeyId')

        # Don't check "is not None" because we want to allow users to set an
        # empty string as a means to pull credentials from Amazon.
        if aws_profile:
            LOGGER.debug(f'Using AWS profile "{aws_profile}".')
            self._s3_client = boto3.Session(
                profile_name=aws_profile).client('s3')
        elif access_key_id is not None:
            secret_access_key = config.get_option('s3.secretAccessKey')
            self._s3_client = boto3.client(
                's3',
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key)
        else:
            LOGGER.debug(f'Using default AWS profile.')
            self._s3_client = boto3.client('s3')

    @run_on_executor
    def _get_static_upload_files(self):
        """Return a list of static files to upload.

        Returns an empty list if the files are already uploaded.
        """
        try:
            self._s3_client.head_object(
                Bucket=self._bucketname,
                Key=self._s3_key('index.html'))
            return []
        except botocore.exceptions.ClientError:
            return list(self._static_files)

    @run_on_executor
    def _bucket_exists(self):
        # THIS DOES NOT WORK because the aws exception isn't being
        # caught and disappearing.
        try:
            self._s3_client.head_bucket(Bucket=self._bucketname)
        except botocore.exceptions.ClientError:
            LOGGER.debug('"%s" bucket not found', self._bucketname)
            return False
        return True

    @run_on_executor
    def _create_bucket(self):
        LOGGER.debug('Attempting to create "%s" bucket', self._bucketname)
        self._s3_client.create_bucket(
            ACL='public-read',
            Bucket=self._bucketname,
            CreateBucketConfiguration={'LocationConstraint': self._region})
        LOGGER.debug('"%s" bucket created', self._bucketname)

    @gen.coroutine
    def _s3_init(self):
        """Initialize s3 bucket."""
        assert config.get_option('s3.sharingEnabled'), (
            'Sharing is disabled. See "s3.sharingEnabled".')
        try:
            bucket_exists = yield self._bucket_exists()
            if not bucket_exists:
                yield self._create_bucket()

        except botocore.exceptions.NoCredentialsError:
            LOGGER.error(
                'please set "AWS_ACCESS_KEY_ID" and "AWS_SECRET_ACCESS_KEY" '
                'environment variables')
            raise errors.S3NoCredentials

    def _s3_key(self, relative_path, add_prefix=True):
        """Convert a local file path into an s3 key (ie path)."""
        key = os.path.join(self._release_hash, relative_path)
        if add_prefix:
            key = os.path.join(self._key_prefix, key)
        return os.path.normpath(key)

    @gen.coroutine
    def _save_report_files(self, report_id, files, progress_coroutine=None,
            manifest_save_order=None):
        """Save files related to a given report.

        See AbstractStorage for docs.
        """
        yield self._s3_init()
        static_files = yield self._get_static_upload_files()
        files_to_upload = static_files + files

        if manifest_save_order is not None:
            manifest_index = None
            manifest_tuple = None
            for i, file_tuple in enumerate(files_to_upload):
                if file_tuple[0] == 'manifest.json':
                    manifest_index = i
                    manifest_tuple = file_tuple
                    break

            if manifest_tuple:
                files_to_upload.pop(manifest_index)

                if manifest_save_order == 'first':
                    files_to_upload.insert(0, manifest_tuple)
                else:
                    files_to_upload.append(manifest_tuple)

        yield self._s3_upload_files(files_to_upload, progress_coroutine)

        raise gen.Return('%s?id=%s' % (self._web_app_url, report_id))

    @gen.coroutine
    def _s3_upload_files(self, files, progress_coroutine):
        for i, (path, data) in enumerate(files):
            mime_type = mimetypes.guess_type(path)[0]
            if not mime_type:
                mime_type = 'application/octet-stream'
            self._s3_client.put_object(
                Bucket=self._bucketname,
                Body=data,
                Key=self._s3_key(path),
                ContentType=mime_type,
                ACL='public-read')
            LOGGER.debug('Uploaded: "%s"' % path)

            if progress_coroutine:
                yield progress_coroutine(math.ceil(100 * (i + 1) / len(files)))
            else:
                yield
