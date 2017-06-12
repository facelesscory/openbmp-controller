from confluent_kafka import Consumer, KafkaError
import yaml, time, datetime, json, ipaddress, pdb
import threading, Queue, argparse
from rib import RIB, LocalRib, AdjRibPostPolicy
import signal, sys
from functools import partial
import redis

import logging, logging.handlers
logging.basicConfig()
logger = logging.getLogger()
logger.setLevel(logging.INFO)

from openbmp.api.parsed.message import Message
from openbmp.api.parsed.message import Peer
from openbmp.api.parsed.message import Router
from openbmp.api.parsed.message import UnicastPrefix

PREFIX_MSG_DAMPENING_TIMER=2
PEER_MSG_DAMPENING_TIMER=2

class BGPPeer(object):
    def __init__(self,
                 remote_asn=None,
                 local_asn=None,
                 data=None):
        self.remote_asn = remote_asn
        self.local_asn = local_asn
        self.data = {}
        if data is not None:
            self.data.update(data)

    def serialize(self):
        peer = {'remote_asn' : self.remote_asn,
                 'local_asn' : self.local_asn,
                 'attributes': self.data}
        return json.dumps(peer)

class Node(object):
    def __init__(self,
                 node_hash=None,
                 name=None,
                 ipaddr=None,
                 data=None):
        self.peers = {}
        self.hash = node_hash
        self.name = name
        self.ipaddr = ipaddr
        self.data = {}
        self.adjInRib = RIB()
        self.adjInRibPP = AdjRibPostPolicy()
        self.localRib = LocalRib()

        if data is not None:
            self.data.update(data)

        self.dispatch = {'up' : self.add_peer,
                         'down' : self.delete_peer}

    def serialize(self):
        peerset = {}
        for peer in self.peers.keys():
            peerset.update({ peer : self.peers[peer].serialize()})

        node = {'name' : self.name,
                'ipaddr' : self.ipaddr,
                'peers': peerset,
                'adjInRib' : self.adjInRib.serialize(),
                'adjInRibPP' : self.adjInRibPP.serialize()}
        return node
 
    def add_peer(self, peer_msg):
        if str(peer_msg['hash']) not in self.peers:
            # Create the peer object
            peer = BGPPeer(remote_asn = peer_msg.pop('remote_asn'),
                           local_asn = peer_msg.pop('local_asn'),
                           data=peer_msg)

            # Add to existing peer set
            self.peers.update({str(peer_msg['hash']) : peer})

        else:
            logger.debug("Received an add event for an existing peer. Strange, but ignore")

    def delete_peer(self, peer_msg):
        if str(peer_msg['hash']) in self.peers:
            # Delete the particular peer from the peer set
            del self.peers[peer_msg['hash']]
        else:
            logger.debug("Received a del event for a non-existent peer, ignore")


    def process_msg(self, peer_msg):
        # Callback invoked by the consumer when a peer message is received over openbmp
        self.dispatch[str(peer_msg['action'])](peer_msg)


class BMPNodes(object):
    def __init__(self, bootstrap_server=None, redishost=None):
        self.nodes = {}
        if redishost is None:
           raise ValueError("Redis Hostname not specified, bailing out")
        else:
            self.redis = redis.StrictRedis(host=redishost)
            self.redis.flushall()
            self.pubsub = self.redis.pubsub()

        self.event = threading.Event()
        self.threadList = []
        self.poisonpillq = Queue.Queue()
        self.peer_consumer = None
        self.router_consumer = None

        if bootstrap_server is not None:
            self.bootstrap_server = bootstrap_server

            for fn in [self.capture_router_msg,
                       self.capture_peer_msg,
                       self.capture_prefix_msg,
                       self.redis_listener]:
                thread = threading.Thread(target=fn, args=())
                self.threadList.append(thread)
                thread.daemon = True                            # Daemonize thread
                thread.start()                                  # Start the execution
        else:
            raise ValueError("Bootstrap server not specified")

        self.dispatch = {'init' : self.add_router,
                         'term' : self.delete_router}

        self.redis_dispatch = {'AdjInRib' : self.adjRibPolicyWorker,
                               'AdjInRibPP' : self.localRibWorker}


    def get_nodes(self):
        nodeset = {}
        for node in self.nodes.keys():
            rtr = self.nodes[node]
            nodeset.update({str(rtr.name)+':'+str(rtr.ipaddr) : node})
            # Also provide the reverse mapping
            nodeset.update({node : str(rtr.name)+':'+str(rtr.ipaddr)})
        return nodeset

    def serialize(self):
        nodeset = {}
        for node in self.nodes.keys():
            nodeset.update({node : self.nodes[node].serialize()})

        return nodeset
            
    class PoisonPillException(Exception):
        pass

    def consumer_cleanup(self):
        logger.info("Cleaning up, exiting the active threads")
        for thread in self.threadList:
            self.poisonpillq.put("quit")

        for thread in self.threadList:
            logger.info("Waiting for %s to finish..." %(thread.name))
            thread.join()
        return


    def process_msg(self, router_msg):
        # Ignore the first message (action = first)
        for msg in router_msg:
            if str(msg['action']) != 'first':
                self.dispatch[str(msg['action'])](msg)
            else:
                logger.debug("Ignoring action=first in openbmp router message")


    def add_router(self, router_msg):
        if str(router_msg['hash']) not in self.nodes:
            # Create the router object
            node = Node(node_hash = router_msg['hash'],
                        name = router_msg.pop('name'),
                        ipaddr = router_msg.pop('ip_address'),
                        data=router_msg)

            # Add to existing router set
            self.nodes.update({str(router_msg['hash']) : node})

        else:
            logger.debug("Received an add event for an existing peer. Strange, but ignore")

    def delete_router(self, router_msg):
        if str(router_msg['hash']) in self.nodes:
            # Delete the particular router from the current router set
            del self.nodes[str(router_msg['hash'])]

            # Delete the router hash from redis
            self.redis.delete(str(router_msg['hash']))
        else:
            logger.debug("Received a del event for a non-existent peer, ignore")


    def update_redis(self, channel=None):
        # Called to reflect latest state when new messages are received. 
        nodes = {}
        if self.get_nodes():
            self.redis.hmset("routers", self.get_nodes())    
            for node in self.nodes.keys():
                self.redis.hmset(node, self.nodes[node].serialize())

        if channel:
            # Publish message to redis Listeners
            self.redis.publish(channel,"Publish to "
                                       +str(self.redis_dispatch[channel].__name__)
                                       +" worker")

    def redis_listener(self):
        self.pubsub.subscribe(['AdjInRib', 'AdjInRibPP'])
        pill = ''
        try:
            while True:
                try:
                    pill = self.poisonpillq.get_nowait()
                except Queue.Empty:
                    pass

                if isinstance(pill, str) and pill == "quit":
                    raise self.PoisonPillException

                for item in self.pubsub.listen():
                    print item
                    logger.debug("Received Redis event")
                    if item['data'] == "quit":
                        self.pubsub.unsubscribe()
                        logger.debug("unsubscribed and finished redis pubsub listener")
                        raise self.PoisonPillException
                    else:
                        print "item is =" 
                        print item
                        print "redis dispatch is="
                        print self.redis_dispatch

                        if item['channel'] in self.redis_dispatch:
                            print "Here"
                            print item['channel']
                            print self.redis_dispatch[item['channel']].__name__
                            self.redis_dispatch[item['channel']]()

        except self.PoisonPillException:
            print "Poison Pill received"
            print "Quitting the redis Listener thread"
            return

        except Exception as e:
            print "Exception occurred while listening for redis events"
            print "Error is " +str(e)
            return


    def adjRibPolicyWorker(self):
        logger.debug("Received an AdjInRib event")
        # walk through the nodes and apply available policies 
        nodes = {}
        if self.get_nodes():
            for node in self.nodes.keys():
                # process and apply policies
                self.nodes[node].adjInRibPP.process_adjInRib(node, self.redis)


    def localRibWorker(self):
        print "Received an AdjInRibPP event"
            # walk through the nodes and apply available path selection algorithms
        nodes = {}
        if self.get_nodes():
            for node in self.nodes.keys():
               # process and do path selection
                self.nodes[node].localRib.process_adjInRibPP(node, self.redis)

 
    def capture_router_msg(self):
        pill = ''
        topics = ['openbmp.parsed.router']
        logger.debug("Connecting to Kafka to receive router messages")
        self.router_consumer = Consumer({'bootstrap.servers': self.bootstrap_server, 'group.id': 'bmp_client'+str(time.time()),
                                         'client.id': 'bmp_client'+str(time.time()),
                                         'default.topic.config': {'auto.offset.reset': 'smallest',
                                                                  'auto.commit.interval.ms': 1000,
                                                                  'enable.auto.commit': True }})

        self.router_consumer.subscribe(topics)

        try:
            while True:
                msg = self.router_consumer.poll(timeout=1.0)

                try:
                    pill = self.poisonpillq.get_nowait()
                except Queue.Empty:
                    pass

                if isinstance(pill, str) and pill == "quit":
                    raise self.PoisonPillException

                if msg is None:
                    self.event.set()
                    continue
                if msg.error():
                    # Error or event
                    if msg.error().code() == KafkaError._PARTITION_EOF:
                        # End of partition event
                        logger.debug('%% %s [%d] reached end at offset %d\n' %
                                         (msg.topic(), msg.partition(), msg.offset()))
                    elif msg.error():
                        # Error
                        raise KafkaException(msg.error())
                else:
                    # Process the  message
                    m = Message(msg.value())  # Gets body of kafka message.
                    t = msg.topic()  # Gets topic of kafka message.
                    m_tag = t.split('.')[2].upper()
                    t_stamp = str(datetime.datetime.now())

                    if t == "openbmp.parsed.router":
                        router = Router(m)
                        logger.debug('Received Message (' + t_stamp + ') : ' + m_tag + '(V: ' + str(m.version) + ')')
                        logger.debug(router.to_json_pretty())
                        router_msg = json.loads(router.to_json_pretty())
                        logger.debug("Calling process msg for Router messages")
                        bmpnodes.process_msg(router_msg)
                        # update redis 
                        self.update_redis()
                        self.event.clear()

        except self.PoisonPillException:
            logger.debug("Poison Pill received")
            logger.debug("Shutting down the router message consumer")
            self.router_consumer.close()
            return

        except Exception as e:
            logger.debug("Exception occurred while listening for router messages")
            logger.debug("Error is " +str(e))
            self.router_consumer.close()
            return


    def capture_peer_msg(self):

        pill = ''
        topics = ['openbmp.parsed.peer']
        logger.info("Connecting to Kafka to receive peer messages")
        self.peer_consumer = Consumer({'bootstrap.servers': self.bootstrap_server, 'group.id': 'bmp_client'+str(time.time()),
                                       'client.id': 'bmp_client'+str(time.time()),
                                       'default.topic.config': {'auto.offset.reset': 'smallest',
                                                                'auto.commit.interval.ms': 1000,
                                                                'enable.auto.commit': True }})

        self.peer_consumer.subscribe(topics)

        try:
            while True:
                msg = self.peer_consumer.poll(timeout=1.0)

                try:
                    pill = self.poisonpillq.get_nowait()
                except Queue.Empty:
                    pass

                if isinstance(pill, str) and pill == "quit":
                    raise self.PoisonPillException


                if msg is None:
                    continue
                if msg.error():
                    # Error or event
                    if msg.error().code() == KafkaError._PARTITION_EOF:
                        # End of partition event
                        logger.debug('%% %s [%d] reached end at offset %d\n' %
                                         (msg.topic(), msg.partition(), msg.offset()))
                    elif msg.error():
                        # Error
                        raise KafkaException(msg.error())
                else:
                    # Process the  message
                    m = Message(msg.value())  # Gets body of kafka message.
                    t = msg.topic()  # Gets topic of kafka message.
                    m_tag = t.split('.')[2].upper()
                    t_stamp = str(datetime.datetime.now())


                    if t == "openbmp.parsed.peer":
                        peer = Peer(m)
                        logger.debug('Received Message (' + t_stamp + ') : ' + m_tag + '(V: ' + str(m.version) + ')')
                        logger.debug(peer.to_json_pretty())
                        peer_msg = json.loads(peer.to_json_pretty())
                        for msg in peer_msg:
                            processed = False
                            while not processed:
                                if str(msg['router_hash']) in self.nodes:
                                    self.nodes[str(msg['router_hash'])].process_msg(msg)
                                    processed = True
                                else:
                                    logger.debug("Received peer message for currently unknown Router, hash="+str(msg['router_hash']))
                                    logger.debug("Let's wait for router_msg event to be set")
                                    self.event.wait(PEER_MSG_DAMPENING_TIMER)

                        # Go ahead and update Redis
                        self.update_redis()
 
        except self.PoisonPillException:
            logger.debug("Poison Pill received")
            logger.debug("Shutting down the peer message consumer")
            self.peer_consumer.close()
            return

        except Exception as e:
            logger.debug("Exception occured while listening to peer messages from Kafka")
            logger.debug("Error is "+ str(e))
            self.router_consumer.close()
            return


    def capture_prefix_msg(self):
        pill = ''
        topics = ['openbmp.parsed.unicast_prefix']
        logger.debug("Connecting to Kafka to receive prefix messages")
        self.prefix_consumer = Consumer({'bootstrap.servers': self.bootstrap_server, 'group.id': 'bmp_client'+str(time.time()),
                                         'client.id': 'bmp_client'+str(time.time()),
                                         'default.topic.config': {'auto.offset.reset': 'smallest',
                                                                  'auto.commit.interval.ms': 1000,
                                                                  'enable.auto.commit': True }})

        self.prefix_consumer.subscribe(topics)

        try:
            while True:
                msg = self.prefix_consumer.poll(timeout=1.0)

                try:
                    pill = self.poisonpillq.get_nowait()
                except Queue.Empty:
                    pass

                if isinstance(pill, str) and pill == "quit":
                    raise self.PoisonPillException

                if msg is None:
                    continue
                if msg.error():
                    # Error or event
                    if msg.error().code() == KafkaError._PARTITION_EOF:
                        # End of partition event
                        logger.debug('%% %s [%d] reached end at offset %d\n' %
                                         (msg.topic(), msg.partition(), msg.offset()))
                    elif msg.error():
                        # Error
                        raise KafkaException(msg.error())
                else:
                    # Process the  message
                    m = Message(msg.value())  # Gets body of kafka message.
                    t = msg.topic()  # Gets topic of kafka message.
                    m_tag = t.split('.')[2].upper()
                    t_stamp = str(datetime.datetime.now())

                    if t == "openbmp.parsed.unicast_prefix":
                        unicast_prefix = UnicastPrefix(m)
                        logger.debug('Received Message (' + t_stamp + ') : ' + m_tag + '(V: ' + str(m.version) + ')')
                        logger.debug(unicast_prefix.to_json_pretty())
                        prefix_msg = yaml.safe_load(unicast_prefix.to_json_pretty())

                        for msg in prefix_msg:
                            processed = False
                            while not processed:
                                if str(msg['router_hash']) in self.nodes:
                                    self.nodes[str(msg['router_hash'])].adjInRib.process_msg(msg)
                                    processed = True
                                else:
                                    logger.debug("Received peer message for currently unknown Router, hash="+str(msg['router_hash']))
                                    logger.debug("Let's wait for router_msg event to be set")
                                    self.event.wait(PREFIX_MSG_DAMPENING_TIMER)

                        # Go ahead and update Redis
                        self.update_redis('AdjInRib')
                        self.update_redis('AdjInRibPP')

        except self.PoisonPillException:
            logger.debug("Poison Pill received")
            logger.debug("Shutting down the prefix message consumer")
            self.prefix_consumer.close()
            return

        except Exception as e:
            logger.debug("Exception occurred while listening for prefix messages")
            logger.debug("Error is " +str(e))
            self.prefix_consumer.close()
            return


EXIT_FLAG = False

# POSIX signal handler to ensure we shutdown cleanly
def handler(bmpnodes, signum, frame):
    global EXIT_FLAG

    if not EXIT_FLAG:
        EXIT_FLAG = True
        logger.info("Cleaning up...")
        bmpnodes.consumer_cleanup()
        sys.exit(0)

        
if __name__ == "__main__":


    parser = argparse.ArgumentParser()
    parser.add_argument('-f', '--file', action='store', dest='route_policy',
                    help='Specify the YAML file describing user defined rules for netlink route import')
    parser.add_argument('-i', '--server-ip', action='store', dest='server_ip',
                    help='Specify the IOS-XR GRPC server IP address', required=True)
    parser.add_argument('-p', '--server-port', action='store', dest='server_port',
                    help='Specify the IOS-XR GRPC server port', required=True)
    parser.add_argument('-b', '--bootstrap-server', action='store', dest='bootstrap_server',
                    help='Specify hostname of the kafka cluster', required=True)
    parser.add_argument('-v', '--verbose', action='store_true',
                    help='Enable verbose logging')


    results = parser.parse_args()
    if results.verbose:
        logger.info("Starting verbose debugging")
        logger.setLevel(logging.DEBUG)


    if results.server_ip and results.server_port:
        server_ip = results.server_ip
        server_port = int(results.server_port)


    bootstrap_server = results.bootstrap_server
    bmpnodes = BMPNodes(bootstrap_server, redishost='10.30.110.214')


    pdb.set_trace()
    # Register our handler for keyboard interrupt and termination signals
    signal.signal(signal.SIGINT, partial(handler, bmpnodes))
    signal.signal(signal.SIGTERM, partial(handler, bmpnodes))

    # The process main thread does nothing but wait for signals
    signal.pause()
