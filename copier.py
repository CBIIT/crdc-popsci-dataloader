#!/bin/env python3
import os

from boto3.s3.transfer import TransferConfig
import requests

from bento.common.utils import get_logger, format_bytes, removeTrailingSlash
from bento.common.s3 import S3Bucket



class Copier:
    adapter_attrs = ['load_file_info', 'clear_file_info', 'get_org_url', 'get_file_name', 'get_org_md5',
                     'filter_fields', 'get_fields']

    TRANSFER_UNIT_MB = 1024 * 1024
    MULTI_PART_THRESHOLD = 100 * TRANSFER_UNIT_MB
    MULTI_PART_CHUNK_SIZE = MULTI_PART_THRESHOLD
    PARTS_LIMIT = 900

    # keys for copy result dict
    STATUS = 'status'
    SIZE = 'size'
    MD5 = 'md5'
    KEY = 'key'
    NAME = 'name'
    FIELDS = 'fields'


    def __init__(self, bucket_name, prefix, adapter):

        """"
        Copy file from URL or local file to S3 bucket
        :param bucket_name: string type
        """
        if not bucket_name:
            raise ValueError('Empty destination bucket name')
        self.bucket_name = bucket_name
        self.bucket = S3Bucket(self.bucket_name)

        if prefix and isinstance(prefix, str):
            self.prefix = removeTrailingSlash(prefix)
        else:
            raise ValueError(f'Invalid prefix: "{prefix}"')

        # Verify adapter has all functions needed
        for attr in self.adapter_attrs:
            if not hasattr(adapter, attr):
                raise TypeError(f'Adapter doesn\'t have "{attr}" attribute/method')
        self.adapter = adapter

        self.log = get_logger('Copier')
        self.files_exist_at_dest = 0
        self.files_copied = 0
        self.files_not_found = set()

    def set_bucket(self, bucket_name):
        if bucket_name != self.bucket_name:
            self.bucket_name = bucket_name
            self.bucket = S3Bucket(self.bucket_name)

    def copy_file(self, file_info, overwrite, dryrun):
        try:
            self.adapter.clear_file_info()
            self.adapter.load_file_info(file_info)
            org_url = self.adapter.get_org_url()
            key = f'{self.prefix}/{self.adapter.get_file_name()}'
            succeed = {self.STATUS: True,
                    self.MD5: self.adapter.get_org_md5(),
                    self.NAME: self.adapter.get_file_name(),
                    self.KEY: key,
                    self.FIELDS: self.adapter.get_fields()
                    }

            self.log.info(f'Copying from {org_url} to s3://{self.bucket_name}/{key} ...')

            if not self._file_exists(org_url):
                return {self.STATUS: False}

            org_size = self._get_org_size(org_url)
            if org_size is None:
                self.log.error(f'Could not get original file size!')
                return {self.STATUS: False}

            self.log.info(f'Original file size: {format_bytes(org_size)}.')
            if not overwrite and self.bucket.same_size_file_exists(key, org_size):
                self.log.info(f'Same size file exists at destination: "{key}"')
                self.files_exist_at_dest += 1
                succeed[self.SIZE] = org_size
                return succeed

            if self._is_local(org_url):
                file_path = self._get_local_path(org_url)
                with open(file_path, 'rb') as stream:
                    return self._upload_obj(stream, key, org_size, dryrun, succeed)
            else:
                with requests.get(org_url, stream=True) as r:
                    return self._upload_obj(r.raw, key, org_size, dryrun, succeed)

        except Exception as e:
            self.log.debug(e)
            self.log.error('Copy file failed! Check debug log for detailed information')
            return {self.STATUS: False}

    def _upload_obj(self, stream, key, org_size, dryrun, succeed):
            size = 0
            if not dryrun:
                parts = org_size // self.MULTI_PART_CHUNK_SIZE
                chunk_size = self.MULTI_PART_CHUNK_SIZE if parts < self.PARTS_LIMIT else org_size // self.PARTS_LIMIT

                t_config = TransferConfig(multipart_threshold=self.MULTI_PART_THRESHOLD,
                                          multipart_chunksize=chunk_size)
                self.bucket._upload_file_obj(key, stream, t_config)
                self.files_copied += 1
                self.log.info(f'Copying file {key} SUCCEEDED!')
                size = self.bucket.get_object_size(key)
            else:
                self.log.info(f'Copying file {key} skipped (dry run)')
                size = org_size
            succeed[self.SIZE] = size
            return succeed

    def _get_org_size(self, org_url):
        if self._is_local(org_url):
            file_path = self._get_local_path(org_url)
            return os.path.getsize(file_path)
        else:
            with requests.head(org_url, stream=True) as r:
                if not r.ok:
                    self.log.error(f'Http Error Code {r.status_code} for {org_url}')
                    return None
                if r.headers['Content-length']:
                    return int(r.headers['Content-length'])
                else:
                    return None

    def _file_exists(self, org_url):
        if self._is_local(org_url):
            file_path = self._get_local_path(org_url)
            if not os.path.isfile(file_path):
                self.log.error(f'"{file_path}" is not a file!')
                return False
            else:
                return True
        else:
            with requests.head(org_url) as r:
                if r.ok:
                    return True
                elif r.status_code == 404:
                    self.log.error(f'File not found: {org_url}!')
                    self.files_not_found.add(org_url)
                else:
                    self.log.error(f'Head file error - {r.status_code}: {org_url}')
                return False

    def _is_local(self, org_url):
        return org_url.startswith('file://')

    def _get_local_path(self, org_url):
        if self._is_local(org_url):
            return org_url.replace('file://', '')
        else:
            raise ValueError(f'{org_url} is not a local file!')

