"""
  Implementation for rebalance, add, remove, stop rebalance.
"""

import time
import os
import sys
import util_cli as util
import socket

from usage import usage
from restclient import *
from listservers import *

# the rest commands and associated URIs for various node operations

rest_cmds = {
    'rebalance'         :'/controller/rebalance',
    'rebalance-stop'    :'/controller/stopRebalance',
    'rebalance-status'  :'/pools/default/rebalanceProgress',
    'server-add'        :'/controller/addNode',
    'server-readd'      :'/controller/reAddNode',
    'failover'          :'/controller/failOver',
    'cluster-init'      :'/settings/web',
    'cluster-edit'      :'/settings/web',
    'node-init'         :'/nodes/self/controller/settings',
    'setting-compaction'    :'/controller/setAutoCompaction',
    'setting-notification'  :'/settings/stats',
    'setting-autofailover'  :'/settings/autoFailover',
    'setting-alert'         :'/settings/alerts'
}

server_no_remove = [
    'rebalance-stop',
    'rebalance-status',
    'server-add',
    'server-readd',
    'failover'
]
server_no_add = [
    'rebalance-stop',
    'rebalance-status',
    'failover',
]

# Map of operations and the HTTP methods used against the REST interface

methods = {
    'rebalance'         :'POST',
    'rebalance-stop'    :'POST',
    'rebalance-status'  :'GET',
    'eject-server'      :'POST',
    'server-add'        :'POST',
    'server-readd'      :'POST',
    'failover'          :'POST',
    'cluster-init'      :'POST',
    'cluster-edit'      :'POST',
    'node-init'         :'POST',
    'setting-compaction'    :'POST',
    'setting-notification'  :'POST',
    'setting-autofailover'  :'POST',
    'setting-alert'         :'POST',
}

bool_to_str = lambda value: str(bool(value)).lower()

# Map of HTTP success code, success message and error message for
# handling HTTP response properly

class Node:
    def __init__(self):
        self.rest_cmd = rest_cmds['rebalance-status']
        self.method = 'GET'
        self.debug = False
        self.server = ''
        self.port = ''
        self.user = ''
        self.password = ''
        self.params = {}
        self.output = 'standard'
        self.password_new = None
        self.username_new = None
        self.port_new = None
        self.per_node_quota = None
        self.data_path = None
        self.index_path = None
        self.enable_auto_failover = None
        self.enable_notification = None
        self.autofailover_timeout = None
        self.enable_email_alert = None

        #compaction related settings
        self.compaction_db_percentage = None
        self.compaction_db_size = None
        self.compaction_view_percentage = None
        self.compaction_view_size = None
        self.compaction_period_from = None
        self.compaction_period_to = None
        self.enable_compaction_abort = None
        self.enable_compaction_parallel = None


    def runCmd(self, cmd, server, port,
               user, password, opts):
        self.rest_cmd = rest_cmds[cmd]
        self.method = methods[cmd]
        self.server = server
        self.port = int(port)
        self.user = user
        self.password = password
        servers = self.processOpts(cmd, opts)

        if self.debug:
            print "INFO: servers %s" % servers

        if cmd == 'server-add' and not servers['add']:
            usage("please list one or more --server-add=HOST[:PORT];"
                  " or use -h for more help.")

        if cmd == 'server-readd' and not servers['add']:
            usage("please list one or more --server-add=HOST[:PORT];"
                  " or use -h for more help.")

        if cmd in ('server-add', 'rebalance'):
            self.addServers(servers['add'])
            if cmd == 'rebalance':
                self.rebalance(servers)

        if cmd == 'server-readd':
            self.reAddServers(servers)

        if cmd == 'rebalance-status':
            output_result = self.rebalanceStatus()
            print output_result

        if cmd == 'rebalance-stop':
            output_result = self.rebalanceStop()
            print output_result

        if cmd == 'failover':
            if len(servers['failover']) <= 0:
                usage("please list one or more --server-failover=HOST[:PORT];"
                      " or use -h for more help.")

            self.failover(servers)

        if cmd in ('cluster-init', 'cluster-edit'):
            self.clusterInit()

        if cmd == 'node-init':
            self.nodeInit()

        if cmd == 'setting-compaction':
            self.compaction()

        if cmd == 'setting-notification':
            self.notification()

        if cmd == 'setting-alert':
            self.alert()

        if cmd == 'setting-autofailover':
            self.autofailover()

    def clusterInit(self):
        rest = restclient.RestClient(self.server,
                                     self.port,
                                     {'debug':self.debug})
        if self.port_new:
            rest.setParam('port', self.port_new)
        else:
            rest.setParam('port', 'SAME')
        rest.setParam('initStatus', 'done')
        if self.username_new:
            rest.setParam('username', self.username_new)
        else:
            rest.setParam('username', self.user)
        if self.password_new:
            rest.setParam('password', self.password_new)
        else:
            rest.setParam('password', self.password)

        opts = {
            "error_msg": "unable to init %s" % self.server,
            "success_msg": "init %s" % self.server
        }

        output_result = rest.restCmd(self.method,
                                     self.rest_cmd,
                                     self.user,
                                     self.password,
                                     opts)
        print output_result

        # per node quota unfortunately runs against a different location
        if not self.per_node_quota:
            return

        if self.port_new:
            self.port = int(self.port_new)
        if self.username_new:
            self.user = self.username_new
        if self.password_new:
            self.password = self.password_new

        rest = restclient.RestClient(self.server,
                                     self.port,
                                     {'debug':self.debug})
        if self.per_node_quota:
            rest.setParam('memoryQuota', self.per_node_quota)

        output_result = rest.restCmd(self.method,
                                     '/pools/default',
                                     self.user,
                                     self.password,
                                     opts)
        print output_result


    def nodeInit(self):
        rest = restclient.RestClient(self.server,
                                     self.port,
                                     {'debug':self.debug})
        if self.data_path:
            rest.setParam('db_path', self.data_path)

        if self.index_path:
            rest.setParam('index_path', self.index_path)

        opts = {
            "error_msg": "unable to init %s" % self.server,
            "success_msg": "init %s" % self.server
        }

        output_result = rest.restCmd(self.method,
                                     self.rest_cmd,
                                     self.user,
                                     self.password,
                                     opts)
        print output_result

    def compaction(self):
        rest = restclient.RestClient(self.server,
                                     self.port,
                                     {'debug':self.debug})

        if self.compaction_db_percentage:
            rest.setParam('databaseFragmentationThreshold[percentage]', self.compaction_db_percentage)
        if self.compaction_db_size:
            self.compaction_db_size = int(self.compaction_db_size) * 1024**2
            rest.setParam('databaseFragmentationThreshold[size]', self.compaction_db_size)
        if self.compaction_view_percentage:
            rest.setParam('viewFragmentationThreshold[percentage]', self.compaction_view_percentage)
        if self.compaction_view_size:
            self.compaction_view_size = int(self.compaction_view_size) * 1024**2
            rest.setParam('viewFragmentationThreshold[size]', self.compaction_view_size)
        if self.compaction_period_from:
            hour, minute = self.compaction_period_from.split(':')
            if (int(hour) not in range(24)) or (int(minute) not in range(60)):
                print "ERROR: invalid hour or minute value for compaction period"
                return
            else:
                rest.setParam('allowedTimePeriod[fromHour]', int(hour))
                rest.setParam('allowedTimePeriod[fromMinute]', int(minute))
        if self.compaction_period_to:
            hour, minute = self.compaction_period_to.split(':')
            if (int(hour) not in range(24)) or (int(minute) not in range(60)):
                print "ERROR: invalid hour or minute value for compaction"
                return
            else:
                rest.setParam('allowedTimePeriod[toHour]', hour)
                rest.setParam('allowedTimePeriod[toMinute]', minute)
        if self.enable_compaction_abort:
            rest.setParam('allowedTimePeriod[abortOutside]', self.enable_compaction_abort)
        if self.enable_compaction_parallel:
            rest.setParam('parallelDBAndViewCompaction', self.enable_compaction_parallel)
        else:
            self.enable_compaction_parallel = bool_to_str(0)
            rest.setParam('parallelDBAndViewCompaction', self.enable_compaction_parallel)

        if self.compaction_period_from and self.compaction_period_to:
            if self.compaction_period_from >= self.compaction_period_to:
                print "ERROR: compaction from time period cannot be late than to time period"
                return

        opts = {
            "error_msg": "unable to set compaction settings",
            "success_msg": "set compaction settings"
        }
        output_result = rest.restCmd(self.method,
                                     self.rest_cmd,
                                     self.user,
                                     self.password,
                                     opts)
        print output_result

    def notification(self):
        rest = restclient.RestClient(self.server,
                                     self.port,
                                     {'debug':self.debug})
        if self.enable_notification:
            rest.setParam('sendStats', self.enable_notification)

        opts = {
            "error_msg": "unable to set notification settings",
            "success_msg": "set notification settings"
        }
        output_result = rest.restCmd(self.method,
                                     self.rest_cmd,
                                     self.user,
                                     self.password,
                                     opts)
        print output_result

    def alert(self):
        rest = restclient.RestClient(self.server,
                                     self.port,
                                     {'debug':self.debug})
        if self.enable_email_alert:
            rest.setParam('enabled', self.enable_email_alert)

        opts = {
            "error_msg": "unable to set alert settings",
            "success_msg": "set alert settings"
        }
        output_result = rest.restCmd(self.method,
                                     self.rest_cmd,
                                     self.user,
                                     self.password,
                                     opts)
        print output_result

    def autofailover(self):
        rest = restclient.RestClient(self.server,
                                     self.port,
                                     {'debug':self.debug})
        if self.autofailover_timeout:
            if int(self.autofailover_timeout) < 30:
                print "ERROR: Timeout value must be larger than 30 second."
                return
            else:
                rest.setParam('timeout', self.autofailover_timeout)

        if self.enable_auto_failover:
            rest.setParam('enabled', self.enable_auto_failover)

        opts = {
            "error_msg": "unable to set auto failover settings",
            "success_msg": "set auto failover settings"
        }
        output_result = rest.restCmd(self.method,
                                     self.rest_cmd,
                                     self.user,
                                     self.password,
                                     opts)
        print output_result

    def processOpts(self, cmd, opts):
        """ Set standard opts.
            note: use of a server key keeps optional
            args aligned with server.
            """
        servers = {
            'add': {},
            'remove': {},
            'failover': {}
        }

        # don't allow options that don't correspond to given commands

        for o, a in opts:
            usage_msg = "option '%s' is not used with command '%s'" % (o, cmd)

            if o in ( "-r", "--server-remove"):
                if cmd in server_no_remove:
                    usage(usage_msg)
            elif o in ( "-a", "--server-add",
                        "--server-add-username",
                        "--server-add-password"):
                if cmd in server_no_add:
                    usage(usage_msg)

        server = None
        for o, a in opts:
            if o in ("-a", "--server-add"):
                if a == "self":
                    a = socket.gethostbyname(socket.getfqdn())
                server = "%s:%d" % util.hostport(a)
                servers['add'][server] = { 'user':'', 'password':''}
            elif o == "--server-add-username":
                if server is None:
                    usage("please specify --server-add"
                          " before --server-add-username")
                servers['add'][server]['user'] = a
            elif o == "--server-add-password":
                if server is None:
                    usage("please specify --server-add"
                          " before --server-add-password")
                servers['add'][server]['password'] = a
            elif o in ( "-r", "--server-remove"):
                server = "%s:%d" % util.hostport(a)
                servers['remove'][server] = True
                server = None
            elif o in ( "--server-failover"):
                server = "%s:%d" % util.hostport(a)
                servers['failover'][server] = True
                server = None
            elif o in ('-o', '--output'):
                if a == 'json':
                    self.output = a
                server = None
            elif o in ('-d', '--debug'):
                self.debug = True
                server = None
            elif o in ('--cluster-init-password', '--cluster-password'):
                self.password_new = a
            elif o in ('--cluster-init-username', '--cluster-username'):
                self.username_new = a
            elif o in ('--cluster-init-port', '--cluster-port'):
                self.port_new = a
            elif o in ('--cluster-init-ramsize', '--cluster-ramsize'):
                self.per_node_quota = a
            elif o == '--enable-auto-failover':
                self.enable_auto_failover = bool_to_str(a)
            elif o == '--enable-notification':
                self.enable_notification = bool_to_str(a)
            elif o == '--auto-failover-timeout':
                self.autofailover_timeout = a
            elif o == '--compaction-db-percentage':
                self.compaction_db_percentage = a
            elif o == '--compaction-db-size':
                self.compaction_db_size = a
            elif o == '--compaction-view-percentage':
                self.compaction_view_percentage = a
            elif o == '--compaction-view-size':
                self.compaction_view_size = a
            elif o == '--compaction-period-from':
                self.compaction_period_from = a
            elif o == '--compaction-period-to':
                self.compaction_period_to = a
            elif o == '--enable-compaction-abort':
                self.enable_compaction_abort = bool_to_str(a)
            elif o == '--enable-compaction-parallel':
                self.enable_compaction_parallel = bool_to_str(a)
            elif o == '--enable-email-alert':
                self.enable_email_alert = bool_to_str(a)
            elif o == '--node-init-data-path':
                self.data_path = a
            elif o == '--node-init-index-path':
                self.index_path = a

        return servers

    def addServers(self, servers):
        for server in servers:
            user = servers[server]['user']
            password = servers[server]['password']
            output_result = self.serverAdd(server,
                                           user,
                                           password)
            print output_result

    def serverAdd(self, add_server, add_with_user, add_with_password):
        rest = restclient.RestClient(self.server,
                                     self.port,
                                     {'debug':self.debug})
        rest.setParam('hostname', add_server)
        if add_with_user and add_with_password:
            rest.setParam('user', add_with_user)
            rest.setParam('password', add_with_password)

        opts = {}
        opts['error_msg'] = "unable to server-add %s" % add_server
        opts['success_msg'] = "server-add %s" % add_server

        output_result = rest.restCmd('POST',
                                     rest_cmds['server-add'],
                                     self.user,
                                     self.password,
                                     opts)
        return output_result

    def reAddServers(self, servers):
        known_otps, eject_otps, failover_otps, readd_otps = \
            self.getNodeOtps(to_readd=servers['add'])

        for readd_otp in readd_otps:
            rest = restclient.RestClient(self.server,
                                         self.port,
                                         {'debug':self.debug})
            rest.setParam('otpNode', readd_otp)

            opts = {}
            opts['error_msg'] = "unable to re-add %s" % readd_otp
            opts['success_msg'] = "re-add %s" % readd_otp

            output_result = rest.restCmd('POST',
                                         rest_cmds['server-readd'],
                                         self.user,
                                         self.password,
                                         opts)
            print output_result

    def getNodeOtps(self, to_eject=[], to_failover=[], to_readd=[]):
        """ Convert known nodes into otp node id's.
            """
        listservers = ListServers()
        known_nodes_list = listservers.getNodes(
                                listservers.getData(self.server,
                                                    self.port,
                                                    self.user,
                                                    self.password))
        known_otps = []
        eject_otps = []
        failover_otps = []
        readd_otps = []

        for node in known_nodes_list:
            if node.get('otpNode') is None:
                raise Exception("could not access node;" +
                                " please check your username (-u) and password (-p)")

            known_otps.append(node['otpNode'])
            if node['hostname'] in to_eject:
                eject_otps.append(node['otpNode'])
            if node['hostname'] in to_failover:
                if node['clusterMembership'] != 'active':
                    raise Exception('node %s is not active' % node['hostname'])
                else:
                    failover_otps.append(node['otpNode'])
            if node['hostname'] in to_readd:
                readd_otps.append(node['otpNode'])

        return (known_otps, eject_otps, failover_otps, readd_otps)

    def rebalance(self, servers):
        known_otps, eject_otps, failover_otps, readd_otps = \
            self.getNodeOtps(to_eject=servers['remove'])

        rest = restclient.RestClient(self.server,
                                     self.port,
                                     {'debug':self.debug})
        rest.setParam('knownNodes', ','.join(known_otps))
        rest.setParam('ejectedNodes', ','.join(eject_otps))

        opts = {}
        opts['success_msg'] = 'rebalanced cluster'
        opts['error_msg'] = 'unable to rebalance cluster'

        output_result = rest.restCmd('POST',
                                     rest_cmds['rebalance'],
                                     self.user,
                                     self.password,
                                     opts)
        if self.debug:
            print "INFO: rebalance started: %s" % output_result

        sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', 0)

        print "INFO: rebalancing",

        status, error = self.rebalanceStatus(prefix='\n')
        while status == 'running':
            print ".",
            time.sleep(0.5)
            try:
                status, error = self.rebalanceStatus(prefix='\n')
            except socket.error:
                time.sleep(2)
                status, error = self.rebalanceStatus(prefix='\n')

        if error:
            print '\n' + error
            sys.exit(1)
        else:
            print '\n' + output_result

    def rebalanceStatus(self, prefix=''):
        rest = restclient.RestClient(self.server,
                                     self.port,
                                     {'debug':self.debug})
        opts = { 'error_msg':'unable to obtain rebalance status'}

        output_result = rest.restCmd('GET',
                                     rest_cmds['rebalance-status'],
                                     self.user,
                                     self.password,
                                     opts)

        json = rest.getJson(output_result)
        if type(json) == type(list()):
            print prefix + ("ERROR: %s" % json[0])
            sys.exit(1)

        if 'errorMessage' in json:
            error_message = json['errorMessage']
        else:
            error_message = None

        return json['status'],error_message

    def rebalanceStop(self):
        rest = restclient.RestClient(self.server,
                                     self.port,
                                     {'debug':self.debug})

        opts = {}
        opts['success_msg'] = 'rebalance cluster stopped'
        opts['error_msg'] = 'unable to stop rebalance'

        output_result = rest.restCmd('POST',
                                     rest_cmds['rebalance-stop'],
                                     self.user,
                                     self.password,
                                     opts)
        return output_result


    def failover(self, servers):
        known_otps, eject_otps, failover_otps, readd_otps = \
            self.getNodeOtps(to_failover=servers['failover'])

        if len(failover_otps) <= 0:
            usage("specified servers are not part of the cluster: %s" %
                  servers['failover'].keys())

        for failover_otp in failover_otps:
            rest = restclient.RestClient(self.server,
                                         self.port,
                                         {'debug':self.debug})
            rest.setParam('otpNode', failover_otp)

            opts = {}
            opts['error_msg'] = "unable to failover %s" % failover_otp
            opts['success_msg'] = "failover %s" % failover_otp

            output_result = rest.restCmd('POST',
                                         rest_cmds['failover'],
                                         self.user,
                                         self.password,
                                         opts)
            print output_result

