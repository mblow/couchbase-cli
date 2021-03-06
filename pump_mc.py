#!/usr/bin/env python

import logging
import json
import socket
import struct
import time
import sys

import cb_bin_client
import couchbaseConstants
import pump
import snappy # pylint: disable=import-error
from cb_util import tag_user_data

try:
    import ctypes
except ImportError:
    cb_path = '/opt/couchbase/lib/python'
    while cb_path in sys.path:
        sys.path.remove(cb_path)
    try:
        import ctypes
    except ImportError:
        sys.exit('error: could not import ctypes module')
    else:
        sys.path.insert(0, cb_path)

OP_MAP = {
    'get': couchbaseConstants.CMD_GET,
    'set': couchbaseConstants.CMD_SET,
    'add': couchbaseConstants.CMD_ADD,
    'delete': couchbaseConstants.CMD_DELETE,
    }

OP_MAP_WITH_META = {
    'get': couchbaseConstants.CMD_GET,
    'set': couchbaseConstants.CMD_SET_WITH_META,
    'add': couchbaseConstants.CMD_ADD_WITH_META,
    'delete': couchbaseConstants.CMD_DELETE_WITH_META
    }


def to_bytes(bytes_or_str):
    if isinstance(bytes_or_str, str):
        value = bytes_or_str.encode()  # uses 'utf-8' for encoding
    else:
        value = bytes_or_str
    return value  # Instance of bytes


class MCSink(pump.Sink):
    """Dumb client sink using binary memcached protocol.
       Used when moxi or memcached is destination."""

    def __init__(self, opts, spec, source_bucket, source_node,
                 source_map, sink_map, ctl, cur):
        super(MCSink, self).__init__(opts, spec, source_bucket, source_node,
                                     source_map, sink_map, ctl, cur)

        self.op_map = OP_MAP
        if opts.extra.get("try_xwm", 1):
            self.op_map = OP_MAP_WITH_META
        self.conflict_resolve = opts.extra.get("conflict_resolve", 1)
        self.lww_restore = 0
        self.init_worker(MCSink.run)
        self.uncompress = opts.extra.get("uncompress", 0)

        if self.get_conflict_resolution_type() == "lww":
            self.lww_restore = 1

    def close(self):
        self.push_next_batch(None, None)

    @staticmethod
    def check_base(opts, spec):
        if getattr(opts, "destination_vbucket_state", "active") != "active":
            return ("error: only --destination-vbucket-state=active" +
                    " is supported by this destination: %s") % (spec)

        op = getattr(opts, "destination_operation", None)
        if not op in [None, 'set', 'add', 'get']:
            return ("error: --destination-operation unsupported value: %s" +
                    "; use set, add, get") % (op)

        # Skip immediate superclass Sink.check_base(),
        # since MCSink can handle different destination operations.
        return pump.EndPoint.check_base(opts, spec)

    @staticmethod
    def run(self):
        """Worker thread to asynchronously store batches into sink."""

        mconns = {} # State kept across scatter_gather() calls.
        backoff_cap = self.opts.extra.get("backoff_cap", 10)
        while not self.ctl['stop']:
            batch, future = self.pull_next_batch()
            if not batch:
                self.future_done(future, 0)
                self.close_mconns(mconns)
                return

            backoff = 0.1 # Reset backoff after a good batch.

            while batch:  # Loop in case retry is required.
                rv, batch, need_backoff = self.scatter_gather(mconns, batch)
                if rv != 0:
                    self.future_done(future, rv)
                    self.close_mconns(mconns)
                    return

                if batch:
                    self.cur["tot_sink_retry_batch"] = \
                        self.cur.get("tot_sink_retry_batch", 0) + 1

                if need_backoff:
                    backoff = min(backoff * 2.0, backoff_cap)
                    logging.warn("backing off, secs: %s" % (backoff))
                    time.sleep(backoff)

            self.future_done(future, 0)

        self.close_mconns(mconns)

    def get_conflict_resolution_type(self):
        bucket = self.sink_map["buckets"][0]
        confResType = "seqno"
        if "conflictResolutionType" in bucket:
            confResType = bucket["conflictResolutionType"]
        return confResType

    def close_mconns(self, mconns):
        for k, conn in mconns.items():
            self.add_stop_event(conn)
            conn.close()

    def scatter_gather(self, mconns, batch):
        conn = mconns.get("conn")
        if not conn:
            rv, conn = self.connect()
            if rv != 0:
                return rv, None
            mconns["conn"] = conn

        # TODO: (1) MCSink - run() handle --data parameter.

        # Scatter or send phase.
        rv = self.send_msgs(conn, batch.msgs, self.operation())
        if rv != 0:
            return rv, None, None

        # Gather or recv phase.
        rv, retry, refresh = self.recv_msgs(conn, batch.msgs)
        if refresh:
            self.refresh_sink_map()
        if retry:
            return rv, batch, True

        return rv, None, None

    def send_msgs(self, conn, msgs, operation, vbucket_id=None):
        m = []

        msg_format_length = 0
        for i, msg in enumerate(msgs):
            if not msg_format_length:
                msg_format_length = len(msg)
            cmd, vbucket_id_msg, key, flg, exp, cas, meta, val = msg[:8]
            seqno = dtype = nmeta = conf_res = 0
            if msg_format_length > 8:
                seqno, dtype, nmeta, conf_res = msg[8:]
            if vbucket_id is not None:
                vbucket_id_msg = vbucket_id

            if self.skip(key, vbucket_id_msg):
                continue

            if cmd == couchbaseConstants.CMD_SUBDOC_MULTIPATH_MUTATION:
                err, req = self.format_multipath_mutation(key, val, vbucket_id, cas, i)
                if err != 0:
                    return err
                self.append_req(m, req)
                continue
            if cmd == couchbaseConstants.CMD_SUBDOC_MULTIPATH_LOOKUP:
                err, req = self.format_multipath_lookup(key, val, vbucket_id, cas, i)
                if err != 0:
                    return err
                self.append_req(m, req)
                continue

            rv, cmd = self.translate_cmd(cmd, operation, meta)
            if rv != 0:
                return rv
            if dtype > 2:
                if self.uncompress and val:
                    try:
                        val = snappy.uncompress(val)
                    except Exception as err:
                        pass
            if cmd == couchbaseConstants.CMD_GET:
                val, flg, exp, cas = '', 0, 0, 0
            if cmd == couchbaseConstants.CMD_NOOP:
                key, val, flg, exp, cas = '', '', 0, 0, 0
            if cmd == couchbaseConstants.CMD_DELETE:
                val = ''
            # A tombstone can contain Xattrs
            if cmd == couchbaseConstants.CMD_DELETE_WITH_META and not dtype & couchbaseConstants.DATATYPE_HAS_XATTR:
                val = ''
            rv, req = self.cmd_request(cmd, vbucket_id_msg, key, val,
                                       ctypes.c_uint32(flg).value,
                                       exp, cas, meta, i, dtype, nmeta,
                                       conf_res)
            if rv != 0:
                return rv

            self.append_req(m, req)

        if m:
            try:
                conn.s.sendall(self.join_str_and_bytes(m))
            except socket.error as e:
                return "error: conn.sendall() exception: %s" % (e)

        return 0

    @staticmethod
    def format_multipath_mutation(key, value, vbucketId, cas=0, opaque=0):
        if 'obj' not in value:
            return 'value has invalid format for multipath mutation', None
        if 'xattr_f' not in value:
            return 'value has invalid format for multipath mutation', None
        if 'xattr_v' not in value:
            return 'value has invalid format for multipath mutation', None

        key = to_bytes(key)
        obj = to_bytes(value['obj'])
        xattr_f = to_bytes(value['xattr_f'])
        xattr_v = to_bytes(value['xattr_v'])

        subop_format = ">BBHI"
        sbcmd_len = 8 * 2 + len(obj) + len(xattr_f) + len(xattr_v)
        total_body_len = sbcmd_len + 1 +len(key)
        subcmd_msg0 = struct.pack(subop_format, couchbaseConstants.CMD_SUBDOC_DICT_UPSERT,
                                  couchbaseConstants.SUBDOC_FLAG_XATTR_PATH, len(xattr_f), len(xattr_v))
        subcmd_msg0 += xattr_f + xattr_v
        subcmd_msg1 = struct.pack(subop_format, couchbaseConstants.CMD_SET, 0,
                                 0, len(obj))
        subcmd_msg1 += obj

        msg_head = struct.pack(couchbaseConstants.REQ_PKT_FMT, couchbaseConstants.REQ_MAGIC_BYTE,
                               couchbaseConstants.CMD_SUBDOC_MULTIPATH_MUTATION, len(key),
                               1, 0, vbucketId, total_body_len, opaque, cas)
        extras = struct.pack(">B", couchbaseConstants.SUBDOC_FLAG_MKDOC)

        return 0, (msg_head+extras+key+subcmd_msg0+subcmd_msg1, None, None, None, None)

    @staticmethod
    def format_multipath_lookup(key, value, vbucketId, cas=0, opaque=0):
        if 'xattr_f' not in value:
            return 'value has invalid format for multipath lookup', None

        key = to_bytes(key)
        field = to_bytes(value['xattr_f'])

        subcmd_fmt = '>BBH'
        subcmd_msg0 = struct.pack(subcmd_fmt, couchbaseConstants.CMD_SUBDOC_GET,
                                  couchbaseConstants.SUBDOC_FLAG_XATTR_PATH, len(field))
        subcmd_msg0 += field
        subcmd_msg1 = struct.pack(subcmd_fmt, couchbaseConstants.CMD_GET, 0, 0)

        total_body_len = len(subcmd_msg0) +len(subcmd_msg1) + len(key)
        msg_head = struct.pack(couchbaseConstants.REQ_PKT_FMT, couchbaseConstants.REQ_MAGIC_BYTE,
                               couchbaseConstants.CMD_SUBDOC_MULTIPATH_LOOKUP, len(key),
                               0, 0, vbucketId, total_body_len, opaque, cas)

        return 0, (msg_head+key+subcmd_msg0+subcmd_msg1, None, None, None, None)

    @staticmethod
    def join_str_and_bytes(lst):
        out = b''
        for x in lst:
            out += to_bytes(x)
        return out

    def recv_msgs(self, conn, msgs, vbucket_id=None, verify_opaque=True):
        refresh = False
        retry = False

        for i, msg in enumerate(msgs):
            cmd, vbucket_id_msg, key, flg, exp, cas, meta, val = msg[:8]
            if vbucket_id is not None:
                vbucket_id_msg = vbucket_id

            if self.skip(key, vbucket_id_msg):
                continue

            try:
                r_cmd, r_status, r_ext, r_key, r_val, r_cas, r_opaque = \
                    self.read_conn(conn)
                if verify_opaque and i != r_opaque:
                    return "error: opaque mismatch: %s %s" % (i, r_opaque), None, None

                if r_status == couchbaseConstants.ERR_SUCCESS:
                    continue
                elif r_status == couchbaseConstants.ERR_KEY_EEXISTS:
                    #logging.warn("item exists: %s, key: %s" %
                    #             (self.spec, tag_user_data(key)))
                    continue
                elif r_status == couchbaseConstants.ERR_KEY_ENOENT:
                    if (cmd != couchbaseConstants.CMD_TAP_DELETE and
                        cmd != couchbaseConstants.CMD_GET):
                        logging.warn("item not found: %s, key: %s" %
                                     (self.spec, tag_user_data(key)))
                    continue
                elif (r_status == couchbaseConstants.ERR_ETMPFAIL or
                      r_status == couchbaseConstants.ERR_EBUSY or
                      r_status == couchbaseConstants.ERR_ENOMEM):
                    retry = True # Retry the whole batch again next time.
                    continue     # But, finish recv'ing current batch.
                elif r_status == couchbaseConstants.ERR_NOT_MY_VBUCKET:
                    msg = ("received NOT_MY_VBUCKET;"
                           " perhaps the cluster is/was rebalancing;"
                           " vbucket_id: %s, key: %s, spec: %s, host:port: %s:%s"
                           % (vbucket_id_msg, tag_user_data(key), self.spec,
                              conn.host, conn.port))
                    if self.opts.extra.get("nmv_retry", 1):
                        logging.warn("warning: " + msg)
                        refresh = True
                        retry = True
                        self.cur["tot_sink_not_my_vbucket"] = \
                            self.cur.get("tot_sink_not_my_vbucket", 0) + 1
                    else:
                        return "error: " + msg, None, None
                elif r_status == couchbaseConstants.ERR_UNKNOWN_COMMAND:
                    if self.op_map == OP_MAP:
                        if not retry:
                            return "error: unknown command: %s" % (r_cmd), None, None
                    else:
                        if not retry:
                            logging.warn("destination does not take XXX-WITH-META"
                                         " commands; will use META-less commands")
                        self.op_map = OP_MAP
                        retry = True
                elif r_status == couchbaseConstants.ERR_ACCESS:
                    return json.loads(r_val)["error"]["context"], None, None
                else:
                    return "error: MCSink MC error: " + str(r_status), None, None

            except Exception as e:
                logging.error("MCSink exception: %s", e)
                return "error: MCSink exception: " + str(e), None, None
        return 0, retry, refresh

    def translate_cmd(self, cmd, op, meta):
        if len(str(meta)) <= 0:
            # The source gave no meta, so use regular commands.
            self.op_map = OP_MAP

        if cmd in[couchbaseConstants.CMD_TAP_MUTATION, couchbaseConstants.CMD_DCP_MUTATION]:
            m = self.op_map.get(op, None)
            if m:
                return 0, m
            return "error: MCSink.translate_cmd, unsupported op: " + op, None

        if cmd in [couchbaseConstants.CMD_TAP_DELETE, couchbaseConstants.CMD_DCP_DELETE]:
            if op == 'get':
                return 0, couchbaseConstants.CMD_NOOP
            return 0, self.op_map['delete']

        if cmd == couchbaseConstants.CMD_GET:
            return 0, cmd

        return "error: MCSink - unknown cmd: %s, op: %s" % (cmd, op), None

    def append_req(self, m, req):
        hdr, ext, key, val, extra_meta = req
        m.append(hdr)
        if ext:
            m.append(ext)
        if key:
            if isinstance(key, bytes):
                m.append(key.decode())
            else:
                m.append(str(key))
        if val:
            if isinstance(val, bytes):
                m.append(val.decode())
            else:
                m.append(str(val))
        if extra_meta:
            m.append(extra_meta)

    @staticmethod
    def can_handle(opts, spec):
        return (spec.startswith("memcached://") or
                spec.startswith("memcached-binary://"))

    @staticmethod
    def check(opts, spec, source_map):
        host, port, user, pswd, path = \
            pump.parse_spec(opts, spec, int(getattr(opts, "port", 11211)))
        if opts.ssl:
            ports = couchbaseConstants.SSL_PORT
        rv, conn = MCSink.connect_mc(host, port, user, pswd, None, opts.ssl, opts.no_ssl_verify, opts.cacert)
        if rv != 0:
            return rv, None
        conn.close()
        return 0, None

    def refresh_sink_map(self):
        return 0

    @staticmethod
    def consume_design(opts, sink_spec, sink_map,
                       source_bucket, source_map, source_design):
        if source_design:
            logging.warn("warning: cannot restore bucket design"
                         " on a memached destination")
        return 0

    def consume_batch_async(self, batch):
        return self.push_next_batch(batch, pump.SinkBatchFuture(self, batch))

    def connect(self):
        host, port, user, pswd, path = \
            pump.parse_spec(self.opts, self.spec,
                            int(getattr(self.opts, "port", 11211)))
        if self.opts.ssl:
            port = couchbaseConstants.SSL_PORT
        return MCSink.connect_mc(host, port, user, pswd, self.sink_map["name"],
                                 self.opts.ssl, collections=self.opts.collection)

    @staticmethod
    def connect_mc(host, port, username, password, bucket, use_ssl=False, verify=True, ca_cert=None, collections=False):
        username = str(username).encode("ascii")
        password = str(password).encode("ascii")
        if bucket is not None:
            bucket = str(bucket).encode("ascii")
        return pump.get_mcd_conn(host, port, username, password, bucket, use_ssl=use_ssl, verify=verify,
                                 ca_cert=ca_cert, collections=collections)

    def cmd_request(self, cmd, vbucket_id, key, val, flg, exp, cas, meta, opaque, dtype, nmeta, conf_res):
        ext_meta = ''
        if (cmd == couchbaseConstants.CMD_SET_WITH_META or
            cmd == couchbaseConstants.CMD_ADD_WITH_META or
            cmd == couchbaseConstants.CMD_DELETE_WITH_META):

            force = 0
            if int(self.conflict_resolve) == 0:
                force |= 1
            if int(self.lww_restore) == 1:
                force |= 2
            if meta:
                try:
                    ext = struct.pack(">IIQQI", flg, exp, int(str(meta)), cas, force)
                except ValueError:
                    seq_no = str(meta)
                    if len(seq_no) > 8:
                        seq_no = seq_no[0:8]
                    if len(seq_no) < 8:
                        # The seq_no might be 32-bits from 2.0DP4, so pad with 0x00's.
                        seq_no = ('\x00\x00\x00\x00\x00\x00\x00\x00' + seq_no)[-8:]

                    seq_no = seq_no.encode()
                    check_seqno, = struct.unpack(">Q", seq_no)
                    if check_seqno:
                        ext = (struct.pack(">II", flg, exp) + seq_no +
                               struct.pack(">QI", cas, force))
                    else:
                        ext = struct.pack(">IIQQI", flg, exp, 1, cas, force)
            else:
                ext = struct.pack(">IIQQI", flg, exp, 1, cas, force)
            if conf_res:
                extra_meta = struct.pack(">BBHH",
                                couchbaseConstants.DCP_EXTRA_META_VERSION,
                                couchbaseConstants.DCP_EXTRA_META_CONFLICT_RESOLUTION,
                                len(conf_res),
                                conf_res)
                ext += struct.pack(">H", len(extra_meta))
        elif (cmd == couchbaseConstants.CMD_SET or
              cmd == couchbaseConstants.CMD_ADD):
            ext = struct.pack(couchbaseConstants.SET_PKT_FMT, flg, exp)
        elif (cmd == couchbaseConstants.CMD_DELETE or
              cmd == couchbaseConstants.CMD_GET or
              cmd == couchbaseConstants.CMD_NOOP):
            ext = b''
        else:
            return "error: MCSink - unknown cmd for request: " + str(cmd), None

        # Couchase currently allows only the xattr datatype to be set so we need
        # to strip out all of the other datatype flags
        dtype = dtype & couchbaseConstants.DATATYPE_HAS_XATTR

        hdr = self.cmd_header(cmd, vbucket_id, key, val, ext, 0, opaque, dtype)
        return 0, (hdr, ext, key, val, ext_meta)

    def cmd_header(self, cmd, vbucket_id, key, val, ext, cas, opaque,
                   dtype=0,
                   fmt=couchbaseConstants.REQ_PKT_FMT,
                   magic=couchbaseConstants.REQ_MAGIC_BYTE):

        return struct.pack(fmt, magic, cmd,
                           len(key), len(ext), dtype, vbucket_id,
                           len(key) + len(ext) + len(val), opaque, cas)

    def read_conn(self, conn):
        ext = ''
        key = ''
        val = ''

        buf, cmd, errcode, extlen, keylen, data, cas, opaque = \
            self.recv_msg(conn.s, getattr(conn, 'buf', b''))
        conn.buf = buf

        if data:
            ext = data[0:extlen]
            key = data[extlen:extlen+keylen]
            val = data[extlen+keylen:]

        return cmd, errcode, ext, key, val, cas, opaque

    def recv_msg(self, sock, buf):
        pkt, buf = self.recv(sock, couchbaseConstants.MIN_RECV_PACKET, buf)
        if not pkt:
            raise EOFError()
        magic, cmd, keylen, extlen, dtype, errcode, datalen, opaque, cas = \
            struct.unpack(couchbaseConstants.RES_PKT_FMT, pkt)
        if magic != couchbaseConstants.RES_MAGIC_BYTE:
            raise Exception("unexpected recv_msg magic: " + str(magic))
        data, buf = self.recv(sock, datalen, buf)
        return buf, cmd, errcode, extlen, keylen, data, cas, opaque

    def recv(self, skt, nbytes, buf):
        while len(buf) < nbytes:
            data = None
            try:
                data = skt.recv(max(nbytes - len(buf), 4096))
            except socket.timeout:
                logging.error("error: recv socket.timeout")
            except Exception as e:
                logging.error("error: recv exception: " + str(e))

            if not data:
                return None, b''
            buf += data

        return buf[:nbytes], buf[nbytes:]
