from __future__ import division, unicode_literals

import base64
import io
import itertools
import os
import time

from .fragment import FragmentFD
from ..compat import (
    compat_etree_fromstring,
    compat_urlparse,
    compat_urllib_error,
    compat_urllib_parse_urlparse,
)
from ..utils import (
    encodeFilename,
    fix_xml_ampersands,
    sanitize_open,
    struct_pack,
    struct_unpack,
    xpath_text,
)


class FlvReader(io.BytesIO):
    """
    Reader for Flv files
    The file format is documented in https://www.adobe.com/devnet/f4v.html
    """

    # Utility functions for reading numbers and strings
    def read_unsigned_long_long(self):
        return struct_unpack('!Q', self.read(8))[0]

    def read_unsigned_int(self):
        return struct_unpack('!I', self.read(4))[0]

    def read_unsigned_char(self):
        return struct_unpack('!B', self.read(1))[0]

    def read_string(self):
        res = b''
        while True:
            char = self.read(1)
            if char == b'\x00':
                break
            res += char
        return res

    def read_box_info(self):
        """
        Read a box and return the info as a tuple: (box_size, box_type, box_data)
        """
        real_size = size = self.read_unsigned_int()
        box_type = self.read(4)
        header_end = 8
        if size == 1:
            real_size = self.read_unsigned_long_long()
            header_end = 16
        return real_size, box_type, self.read(real_size - header_end)

    def read_asrt(self):
        # version
        self.read_unsigned_char()
        # flags
        self.read(3)
        quality_entry_count = self.read_unsigned_char()
        # QualityEntryCount
        for i in range(quality_entry_count):
            self.read_string()

        segment_run_count = self.read_unsigned_int()
        segments = []
        for i in range(segment_run_count):
            first_segment = self.read_unsigned_int()
            fragments_per_segment = self.read_unsigned_int()
            segments.append((first_segment, fragments_per_segment))

        return {
            'segment_run': segments,
        }

    def read_afrt(self):
        # version
        self.read_unsigned_char()
        # flags
        self.read(3)
        # time scale
        self.read_unsigned_int()

        quality_entry_count = self.read_unsigned_char()
        # QualitySegmentUrlModifiers
        for i in range(quality_entry_count):
            self.read_string()

        fragments_count = self.read_unsigned_int()
        fragments = []
        for i in range(fragments_count):
            first = self.read_unsigned_int()
            first_ts = self.read_unsigned_long_long()
            duration = self.read_unsigned_int()
            if duration == 0:
                discontinuity_indicator = self.read_unsigned_char()
            else:
                discontinuity_indicator = None
            fragments.append({
                'first': first,
                'ts': first_ts,
                'duration': duration,
                'discontinuity_indicator': discontinuity_indicator,
            })

        return {
            'fragments': fragments,
        }

    def read_abst(self):
        # version
        self.read_unsigned_char()
        # flags
        self.read(3)

        self.read_unsigned_int()  # BootstrapinfoVersion
        # Profile,Live,Update,Reserved
        flags = self.read_unsigned_char()
        live = flags & 0x20 != 0
        # time scale
        self.read_unsigned_int()
        # CurrentMediaTime
        self.read_unsigned_long_long()
        # SmpteTimeCodeOffset
        self.read_unsigned_long_long()

        self.read_string()  # MovieIdentifier
        server_count = self.read_unsigned_char()
        # ServerEntryTable
        for i in range(server_count):
            self.read_string()
        quality_count = self.read_unsigned_char()
        # QualityEntryTable
        for i in range(quality_count):
            self.read_string()
        # DrmData
        self.read_string()
        # MetaData
        self.read_string()

        segments_count = self.read_unsigned_char()
        segments = []
        for i in range(segments_count):
            box_size, box_type, box_data = self.read_box_info()
            assert box_type == b'asrt'
            segment = FlvReader(box_data).read_asrt()
            segments.append(segment)
        fragments_run_count = self.read_unsigned_char()
        fragments = []
        for i in range(fragments_run_count):
            box_size, box_type, box_data = self.read_box_info()
            assert box_type == b'afrt'
            fragments.append(FlvReader(box_data).read_afrt())

        return {
            'segments': segments,
            'fragments': fragments,
            'live': live,
        }

    def read_bootstrap_info(self):
        total_size, box_type, box_data = self.read_box_info()
        assert box_type == b'abst'
        return FlvReader(box_data).read_abst()


def read_bootstrap_info(bootstrap_bytes):
    return FlvReader(bootstrap_bytes).read_bootstrap_info()


def build_fragments_list(boot_info):
    """ Return a list of (segment, fragment) for each fragment in the video """
    res = []
    segment_run_table = boot_info['segments'][0]
    fragment_run_entry_table = boot_info['fragments'][0]['fragments']
    first_frag_number = fragment_run_entry_table[0]['first']
    fragments_counter = itertools.count(first_frag_number)
    for segment, fragments_count in segment_run_table['segment_run']:
        for _ in range(fragments_count):
            res.append((segment, next(fragments_counter)))

    if boot_info['live']:
        res = res[-2:]

    return res


def write_unsigned_int(stream, val):
    stream.write(struct_pack('!I', val))


def write_unsigned_int_24(stream, val):
    stream.write(struct_pack('!I', val)[1:])


def write_flv_header(stream):
    """Writes the FLV header to stream"""
    # FLV header
    stream.write(b'FLV\x01')
    stream.write(b'\x05')
    stream.write(b'\x00\x00\x00\x09')
    stream.write(b'\x00\x00\x00\x00')


def write_metadata_tag(stream, metadata):
    """Writes optional metadata tag to stream"""
    SCRIPT_TAG = b'\x12'
    FLV_TAG_HEADER_LEN = 11

    if metadata:
        stream.write(SCRIPT_TAG)
        write_unsigned_int_24(stream, len(metadata))
        stream.write(b'\x00\x00\x00\x00\x00\x00\x00')
        stream.write(metadata)
        write_unsigned_int(stream, FLV_TAG_HEADER_LEN + len(metadata))


def _add_ns(prop):
    return '{http://ns.adobe.com/f4m/1.0}%s' % prop


class F4mFD(FragmentFD):
    """
    A downloader for f4m manifests or AdobeHDS.
    """

    FD_NAME = 'f4m'

    def _get_unencrypted_media(self, doc):
        media = doc.findall(_add_ns('media'))
        if not media:
            self.report_error('No media found')
        for e in (doc.findall(_add_ns('drmAdditionalHeader')) +
                  doc.findall(_add_ns('drmAdditionalHeaderSet'))):
            # If id attribute is missing it's valid for all media nodes
            # without drmAdditionalHeaderId or drmAdditionalHeaderSetId attribute
            if 'id' not in e.attrib:
                self.report_error('Missing ID in f4m DRM')
        media = list(filter(lambda e: 'drmAdditionalHeaderId' not in e.attrib and
                                      'drmAdditionalHeaderSetId' not in e.attrib,
                            media))
        if not media:
            self.report_error('Unsupported DRM')
        return media

    def _get_bootstrap_from_url(self, bootstrap_url):
        bootstrap = self.ydl.urlopen(bootstrap_url).read()
        return read_bootstrap_info(bootstrap)

    def _update_live_fragments(self, bootstrap_url, latest_fragment):
        fragments_list = []
        retries = 30
        while (not fragments_list) and (retries > 0):
            boot_info = self._get_bootstrap_from_url(bootstrap_url)
            fragments_list = build_fragments_list(boot_info)
            fragments_list = [f for f in fragments_list if f[1] > latest_fragment]
            if not fragments_list:
                # Retry after a while
                time.sleep(5.0)
                retries -= 1

        if not fragments_list:
            self.report_error('Failed to update fragments')

        return fragments_list

    def _parse_bootstrap_node(self, node, base_url):
        if node.text is None:
            bootstrap_url = compat_urlparse.urljoin(
                base_url, node.attrib['url'])
            boot_info = self._get_bootstrap_from_url(bootstrap_url)
        else:
            bootstrap_url = None
            bootstrap = base64.b64decode(node.text.encode('ascii'))
            boot_info = read_bootstrap_info(bootstrap)
        return (boot_info, bootstrap_url)

    def real_download(self, filename, info_dict):
        man_url = info_dict['url']
        requested_bitrate = info_dict.get('tbr')
        self.to_screen('[%s] Downloading f4m manifest' % self.FD_NAME)
        urlh = self.ydl.urlopen(man_url)
        man_url = urlh.geturl()
        # Some manifests may be malformed, e.g. prosiebensat1 generated manifests
        # (see https://github.com/rg3/youtube-dl/issues/6215#issuecomment-121704244
        # and https://github.com/rg3/youtube-dl/issues/7823)
        manifest = fix_xml_ampersands(urlh.read()).strip()

        doc = compat_etree_fromstring(manifest)
        formats = [(int(f.attrib.get('bitrate', -1)), f)
                   for f in self._get_unencrypted_media(doc)]
        if requested_bitrate is None:
            # get the best format
            formats = sorted(formats, key=lambda f: f[0])
            rate, media = formats[-1]
        else:
            rate, media = list(filter(
                lambda f: int(f[0]) == requested_bitrate, formats))[0]

        base_url = compat_urlparse.urljoin(man_url, media.attrib['url'])
        bootstrap_node = doc.find(_add_ns('bootstrapInfo'))
        boot_info, bootstrap_url = self._parse_bootstrap_node(bootstrap_node, base_url)
        live = boot_info['live']
        metadata_node = media.find(_add_ns('metadata'))
        if metadata_node is not None:
            metadata = base64.b64decode(metadata_node.text.encode('ascii'))
        else:
            metadata = None

        fragments_list = build_fragments_list(boot_info)
        if self.params.get('test', False):
            # We only download the first fragment
            fragments_list = fragments_list[:1]
        total_frags = len(fragments_list)
        # For some akamai manifests we'll need to add a query to the fragment url
        akamai_pv = xpath_text(doc, _add_ns('pv-2.0'))

        ctx = {
            'filename': filename,
            'total_frags': total_frags,
        }

        self._prepare_frag_download(ctx)

        dest_stream = ctx['dest_stream']

        write_flv_header(dest_stream)
        if not live:
            write_metadata_tag(dest_stream, metadata)

        base_url_parsed = compat_urllib_parse_urlparse(base_url)

        self._start_frag_download(ctx)

        frags_filenames = []
        while fragments_list:
            seg_i, frag_i = fragments_list.pop(0)
            name = 'Seg%d-Frag%d' % (seg_i, frag_i)
            query = []
            if base_url_parsed.query:
                query.append(base_url_parsed.query)
            if akamai_pv:
                query.append(akamai_pv.strip(';'))
            if info_dict.get('extra_param_to_segment_url'):
                query.append(info_dict['extra_param_to_segment_url'])
            url_parsed = base_url_parsed._replace(path=base_url_parsed.path + name, query='&'.join(query))
            frag_filename = '%s-%s' % (ctx['tmpfilename'], name)
            try:
                success = ctx['dl'].download(frag_filename, {'url': url_parsed.geturl()})
                if not success:
                    return False
                (down, frag_sanitized) = sanitize_open(frag_filename, 'rb')
                down_data = down.read()
                down.close()
                reader = FlvReader(down_data)
                while True:
                    _, box_type, box_data = reader.read_box_info()
                    if box_type == b'mdat':
                        dest_stream.write(box_data)
                        break
                if live:
                    os.remove(encodeFilename(frag_sanitized))
                else:
                    frags_filenames.append(frag_sanitized)
            except (compat_urllib_error.HTTPError, ) as err:
                if live and (err.code == 404 or err.code == 410):
                    # We didn't keep up with the live window. Continue
                    # with the next available fragment.
                    msg = 'Fragment %d unavailable' % frag_i
                    self.report_warning(msg)
                    fragments_list = []
                else:
                    raise

            if not fragments_list and live and bootstrap_url:
                fragments_list = self._update_live_fragments(bootstrap_url, frag_i)
                total_frags += len(fragments_list)
                if fragments_list and (fragments_list[0][1] > frag_i + 1):
                    msg = 'Missed %d fragments' % (fragments_list[0][1] - (frag_i + 1))
                    self.report_warning(msg)

        self._finish_frag_download(ctx)

        for frag_file in frags_filenames:
            os.remove(encodeFilename(frag_file))

        return True
