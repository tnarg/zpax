import os.path
import json

from zpax import tzmq, db, node
from paxos import multi, basic

from twisted.internet import defer, task, reactor


_ZPAX_CONFIG_KEY='__zpax_config'
            

class KeyValueDB (node.JSONResponder):
    '''
    This class implements a distributed key=value database that uses
    Paxos to coordinate database updates. Unlike the replacated state
    machine design typically discussed in Paxos literature, this implementation
    takes a simpler approach to ensure consistency. Each key/value update
    includes in the database the Multi-Paxos instance number used to set
    the value for that key. When a node sees an instance number ahead of what
    it thinks is the current number, it continually requests key-value pairs
    with greater instance ids from peer nodes. These are returned in sorted
    order. When the node detects that the current Paxos instance is 1 greater
    than it's most recent database entry, it knows that it is consistent
    with it's peers and will begin participating in Paxos instance resolutions.
    '''
    def __init__(self, node_uid,
                 local_paxos_rep_addr,
                 local_paxos_pub_sub_addr,
                 local_rep_addr,
                 remote_rep_addrs,
                 database_dir,
                 database_filename=None,
                 catchup_retry_delay=2.0,
                 catchup_num_items=2):

        if database_filename is None:
            database_filename = os.path.join(database_dir, 'db.sqlite')

        if database_filename == ':memory:':
            durable_dir = None
            durable_id  = None
        else:
            durable_dir = database_dir
            durable_id  = os.path.basename(database_filename + '.paxos')
            
        
        self.kv_node = KeyValNode(self,
                                  local_paxos_rep_addr,
                                  local_paxos_pub_sub_addr,
                                  durable_dir,
                                  durable_id)

        self.catchup_retry_delay = catchup_retry_delay
        self.catchup_num_items   = catchup_num_items

            
        self.db            = db.DB( database_filename )
        self.db_seq        = self.db.get_last_resolution()
        self.catching_up   = False
        self.catchup_retry = None

        self.rep_addr      = local_rep_addr
            
        self.rep           = tzmq.ZmqRepSocket()
        self.req           = tzmq.ZmqReqSocket()

        self.rep.messageReceived = self._generateResponder( '_REP_' )
        self.req.messageReceived = self._generateResponder( '_REQ_' )
        
        self.rep.bind(self.rep_addr)

        for x in remote_rep_addrs:
            self.req.connect(x)


    def _loadConfiguration(self, cfg_str=None):
        if cfg_str is None:
            cfg_str = self.db.get_value(_ZPAX_CONFIG_KEY)

        cfg = json.loads(cfg_str)
        
        if not self._remote_addrs == set(cfg['pub_addrs']):
            self.connect( cfg['pub_addrs'] )
        


    def initialize(self, quorum_size):
        self.kv_node.initialize(quorum_size)

        
    def connect(self, remote_pub_sub_addrs):
        self._remote_addrs = set( remote_pub_sub_addrs )
        self.kv_node.connect( remote_pub_sub_addrs )


    def shutdown(self):
        self.req.close()
        self.rep.close()
        if self.catchup_retry and self.catchup_retry.active():
            self.catchup_retry.cancel()
        self.kv_node.shutdown()

        
    def getMaxDBSequenceNumber(self):
        return self.db_seq

    
    def isCatchingUp(self):
        return self.catching_up


    def onValueSet(self, key, value, instance_num):
        if key == _ZPAX_CONFIG_KEY:
            self._loadConfiguration( value )
        self.db.update_key( key, value, instance_num )        
        self.db_seq = instance_num


    def catchup(self):
        if self.catching_up or self.db_seq == self.kv_node.currentInstanceNum() - 1:
            return 
        
        self._catchup()

        
    def _catchup(self):
        print '_catchup(): ',
        self.catching_up = self.db_seq != self.kv_node.currentInstanceNum() - 1
        
        if not self.catching_up:
            print '*** CAUGHT UP!! ***'
            return
        
        print 'Requesting next batch from ', self.db_seq

        self.catchup_retry = reactor.callLater(self.catchup_retry_delay,
                                               self._catchup)

        self.req.send( json.dumps( dict(type='catchup_request', last_known_seq=self.db_seq) ) )

        
    #--------------------------------------------------------------------------
    # REQ Socket
    #
    def _REQ_catchup_data(self, msg):
        print 'CATCHUP MSG: ', msg
        print 'Catchup Data({},{}): '.format(msg['from_seq'], self.db_seq),
        print ', '.join( '({}{}{})'.format(*t) for t in msg['key_val_seq_list'] )
        
        if self.catchup_retry and self.catchup_retry.active():
            self.catchup_retry.cancel()
            self.catchup_retry = None
            
        if msg['from_seq'] == self.db_seq:
            for key, val, seq_num in msg['key_val_seq_list']:
                # XXX Convert to updating all keys in one commit
                if key == _ZPAX_CONFIG_KEY:
                    self._loadConfiguration(val)
                self.db.update_key(key, val, seq_num)
                
            self.db_seq = self.db.get_last_resolution()

            print 'LAST RESOLUTION: ', self.db.get_last_resolution()
                    
        self._catchup()

    #--------------------------------------------------------------------------
    # REP Socket
    #           
    def rep_reply(self, **kwargs):
        self.rep.send( json.dumps(kwargs) )

        
    def _REP_propose_value(self, header):
        print 'Proposing ', header
        try:
            jstr = json.dumps( [header['key'], header['value']] )
            self.kv_node.proposeValue(jstr)
            self.rep_reply( proposed = True )
        except node.ProposalFailed, e:
            print 'Proposal FAILED: ', str(e)
            self.rep_reply(proposed=False, message=str(e))

            
    def _REP_query_value(self, header):
        print 'Querying ', header
        print 'DB RESULT: ', self.db.get_value(header['key'])
        self.rep_reply( value = self.db.get_value(header['key']) )


    def _REP_catchup_request(self, header):
        l = list()
        
        for tpl in self.db.iter_updates(header['last_known_seq']):
            l.append( tpl )
            if len(l) == self.catchup_num_items:
                break

        self.rep_reply( type             = 'catchup_data',
                        from_seq         = header['last_known_seq'],
                        key_val_seq_list = l )

        
class KeyValNode (node.BasicNode):
    '''
    This class implements the Paxos logic for KeyValueDB. It extends
    node.BasicNode in two significant ways. First, it embeds within
    each heartbeat message the current multi-paxos sequence number. This
    allows late-joining/recovering nodes to quickly discover that their
    databases are out of sync and begin the catchup process. Second,
    this node drops all Paxos messages while the database is catching
    up. This prevents newly chosen values from entering the database
    prior to the completion of the catchup process. 
    '''

    def __init__(self,
                 kvdb,
                 local_rep_addr,
                 local_pub_sub_addr,
                 durable_dir,
                 object_id):

        super(KeyValNode,self).__init__( local_rep_addr,
                                         local_pub_sub_addr,
                                         durable_dir,
                                         object_id)
        
        self.kvdb = kvdb

        
            
    def getHeartbeatData(self):
        return dict( seq_num = self.currentInstanceNum() )
    
    
    def onHeartbeat(self, data):
        if data['seq_num'] - 1 > self.kvdb.getMaxDBSequenceNumber():
            if data['seq_num'] > self.currentInstanceNum():
                self.slewSequenceNumber( data['seq_num'] )

            self.kvdb.catchup()


    # Override the sequence checking function in the baseclass to cause all
    # packets to be dropped from our Paxos implementation while our database
    # is behind
    def _check_sequence(self, header):
        return super(KeyValNode,self)._check_sequence(header) and not self.kvdb.isCatchingUp()

    
    def onLeadershipAcquired(self):
        print self.node_uid, 'I have the leader!'


    def onLeadershipLost(self):
        print self.node_uid, 'I LOST the leader!'


    def onLeadershipChanged(self, prev_leader_uid, new_leader_uid):
        print '*** Change of guard: ', prev_leader_uid, new_leader_uid


    def onBehindInSequence(self):
        self.kvdb.catchup()

        
    def onProposalResolution(self, instance_num, value):
        # This method is only called when our database is current
        
        print '*** Resolution! ', instance_num, repr(value)

        #print '** DECODED: ', json.loads(value)

        key, value = json.loads(value)

        self.kvdb.onValueSet( key, value, instance_num )
        
            
