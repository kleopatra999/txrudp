"""
RUDP state machine implementation.

Classes:
    RUDPConnection: Endpoint of an RUDP connection
    RUDPConnectionFactory: Creator of RUDPConnections.
"""

import abc
import collections
import json
import random

from twisted.internet import reactor, task

from txrudp import constants, heap, packet


REACTOR = reactor


class RUDPConnection(object):

    """
    A virtual connection over UDP.

    It sequences inbound and outbound packets, acknowledges inbound
    packets, and retransmits lost outbound packets. It may also relay
    packets via other connections, to help with NAT traversal.
    """

    _Address = collections.namedtuple('Address', ['ip', 'port'])

    class ScheduledPacket(object):

        """A packet scheduled for sending or currently in flight."""

        def __init__(self, rudp_packet, timeout, timeout_cb, retries=0):
            """
            Create a new scheduled packet.

            Args:
                rudp_packet: An packet.RUDPPacket in string format.
                timeout: Seconds to wait before activating timeout_cb,
                    as an integer.
                timeout_cb: Callback to invoke upon timer expiration;
                    the callback should implement a `cancel` method.
                retries: Number of times this package has already
                    been sent, as an integer.
            """
            self.rudp_packet = rudp_packet
            self.timeout = timeout
            self.timeout_cb = timeout_cb
            self.retries = retries

        def __repr__(self):
            return '{0}({1}, {2}, {3}, {4})'.format(
                self.__class__.__name__,
                self.rudp_packet,
                self.timeout,
                self.timeout_cb,
                self.retries
            )

    def __init__(self, proto, handler, own_addr, dest_addr, relay_addr=None):
        """
        Create a new connection and register it with the protocol.

        Args:
            proto: Handler to underlying protocol.
            handler: Upstream recipient of received messages and
                handler of other events. Should minimally implement
                `receive_message` and `handle_shutdown`.
            own_addr: Tuple of local host address (ip, port).
            dest_addr: Tuple of remote host address (ip, port).
            relay_addr: Tuple of relay host address (ip, port).

        If a relay address is specified, all outgoing packets are
        sent to that adddress, but the packets contain the address
        of their final destination. This is used for routing.
        """
        self.own_addr = self._Address(*own_addr)
        self.dest_addr = self._Address(*dest_addr)
        if relay_addr is None:
            self.relay_addr = dest_addr
        else:
            self.relay_addr = self._Address(*relay_addr)

        self.connected = False

        self.handler = handler
        self._proto = proto

        self._next_sequence_number = random.randrange(1, 2**16 - 1)
        self._next_expected_seqnum = 0

        self._segment_queue = collections.deque()
        self._sending_window = collections.OrderedDict()

        self._receive_heap = heap.Heap()

        self._looping_send = task.LoopingCall(self._dequeue_outbound_message)
        self._looping_ack = task.LoopingCall(self._send_ack)
        self._looping_receive = task.LoopingCall(self._pop_received_packet)

        # Initiate SYN sequence after receiving any pending SYN message.
        self._syn_handle = REACTOR.callLater(0, self._send_syn)

    def send_message(self, message):
        """
        Send a message to the connected remote host, asynchronously.

        If the message is too large for proper transmission over UDP,
        it is first segmented appropriately.

        Args:
            message: The message to be sent, as a string.
        """
        for segment in self._gen_segments(message):
            self._segment_queue.append(segment)
        self._attempt_enabling_looping_send()

    def receive_packet(self, rudp_packet):
        """
        Called by protocol when a packet arrives for this connection.

        Process received packet and update connection state.

        Silently drop any non-SYN packet if we are disconnected.
        Silently drop any FIN packet if we haven't yet attempted to
        connect.

        NOTE: It is guaranteed that this method will be called
        exactly once for each inbound packet, so it is the ideal
        place to do pre- or post-processing of any RUDPPacket.
        Consider this when subclassing RUDPConnection.

        Args:
            rudp_packet: Received packet.RUDPPacket; it is assumed
                that the packet had already been validated against
                packet.RUDP_PACKET_JSON_SCHEMA.
        """
        if rudp_packet.fin:
            if self.connected or not self._syn_handle.active():
                self._process_fin_packet(rudp_packet)
        elif rudp_packet.syn:
            if not self.connected:
                self._process_syn_packet(rudp_packet)
        elif self.connected:
            self._process_casual_packet(rudp_packet)

    def shutdown(self):
        """
        Terminate connection with remote endpoint.

        1. Send a single FIN packet to remote host.
        2. Stop sending and acknowledging messages.
        3. Cancel all retransmission timers.
        5. Alert handler about connection shutdown.

        The handler should prevent the connection from receiving
        any future messages. The simplest way to do this is to
        remove the connection from the protocol.
        """
        self.connected = False
        self._send_fin()
        if self._looping_ack.running:
            self._looping_ack.stop()
        if self._looping_send.running:
            self._looping_send.stop()
        if self._looping_receive.running:
            self._looping_receive.stop()
        self._clear_sending_window()
        self.handler.handle_shutdown()

    @staticmethod
    def _gen_segments(message):
        """
        Split a message into segments appropriate for transmission.

        Args:
            message: The message to sent, as a string.

        Yields:
            Tuples of two elements; the first element is the number
            of remaining segments, the second is the actual string
            segment.
        """
        max_size = constants.UDP_SAFE_SEGMENT_SIZE
        count = (len(message) + max_size - 1) // max_size
        segments = (
            (count - i - 1, message[i * max_size: (i + 1) * max_size])
            for i in range(count)
        )
        return segments

    def _attempt_enabling_looping_send(self):
        """
        Activate looping send dequeue if a packet can be sent immediately.
        """
        if (
            self.connected and
            not self._looping_send.running and
            len(self._sending_window) < constants.WINDOW_SIZE and
            len(self._segment_queue)
        ):
            self._looping_send.start(0)

    def _attempt_disabling_looping_send(self):
        """
        Deactivate looping send if a packet can't be sent immediately.
        """
        if (
            self._looping_send.running and (
                len(self._sending_window) >= constants.WINDOW_SIZE or
                not len(self._segment_queue)
            )
        ):
            self._looping_send.stop()

    def _get_next_sequence_number(self):
        """Return the next available sequence number."""
        self._next_sequence_number += 1
        return self._next_sequence_number

    def _send_syn(self):
        """
        Create and schedule the initial SYN packet.

        The current ACK number is included; if it is greater than
        0, then this is in effect a SYNACK packet.

        Until successfully acknowledged, all SYN(ACK) packets should
        have the same (initial) sequence number.

        """
        syn_packet = packet.RUDPPacket(
            self._next_sequence_number,
            self.dest_addr,
            self.own_addr,
            ack=self._next_expected_seqnum,
            syn=True
        )
        self._schedule_send_in_order(syn_packet, constants.PACKET_TIMEOUT)

    def _send_ack(self):
        """
        Create and schedule a bare ACK packet.

        Bare ACK packets are sent out-of-order, do not have a
        meaningful sequence number and cannot be ACK-ed. It is of
        no use to retransmit a lost bare ACK packet, since the local
        host's ACK number may have advanced in the meantime. Instead,
        each ACK timeout sends the latest ACK number available.
        """
        ack_packet = packet.RUDPPacket(
            0,
            self.dest_addr,
            self.own_addr,
            ack=self._next_expected_seqnum
        )
        self._schedule_send_out_of_order(ack_packet)

        # NOTE: If the ACK packet is lost, the remote host will
        # retransmit the unacknowledged message and cause the local
        # host to resend the ACK. Therefore, the only meaningful ACK
        # timeout is the keep-alive timeout.
        self._reset_ack_timeout(constants.KEEP_ALIVE_TIMEOUT)

    def _send_fin(self):
        """
        Create and schedule a FIN packet.

        No acknowledgement of this packet is normally expected.
        In addition, no retransmission of this packet is performed;
        if it is lost, the packet timeouts at the remote host will
        cause the connection to be broken. Since the packet is sent
        out-of-order, there is no meaningful sequence number.
        """
        fin_packet = packet.RUDPPacket(
            0,
            self.dest_addr,
            self.own_addr,
            ack=self._next_expected_seqnum,
            fin=True
        )
        self._schedule_send_out_of_order(fin_packet)

    def _schedule_send_out_of_order(self, rudp_packet):
        """
        Schedule a package to be sent out of order.

        Current implementation sends the packet as soon as possible.

        Args:
            rudp_packet: The packet.RUDPPacket to be sent.
        """
        final_packet = self._finalize_packet(rudp_packet)
        self._proto.send_datagram(final_packet, self.relay_addr)

    def _schedule_send_in_order(self, rudp_packet, timeout):
        """
        Schedule a package to be sent and set the timeout timer.

        Args:
            rudp_packet: The packet.RUDPPacket to be sent.
            timeout: The timeout for this packet type.
        """
        final_packet = self._finalize_packet(rudp_packet)
        timeout_cb = REACTOR.callLater(
            0,
            self._do_send_packet,
            rudp_packet.sequence_number
        )
        self._sending_window[rudp_packet.sequence_number] = self.ScheduledPacket(
            final_packet,
            timeout,
            timeout_cb,
            0
        )

    def _dequeue_outbound_message(self):
        """
        Deque a message, wrap it into an RUDP packet and schedule it.

        Pause dequeueing if it would overflow the send window.
        """
        assert self._segment_queue, 'Looping send active despite empty queue.'
        more_fragments, message = self._segment_queue.popleft()

        rudp_packet = packet.RUDPPacket(
            self._get_next_sequence_number(),
            self.dest_addr,
            self.own_addr,
            message,
            more_fragments,
            ack=self._next_expected_seqnum
        )
        self._schedule_send_in_order(rudp_packet, constants.PACKET_TIMEOUT)

        self._attempt_disabling_looping_send()

    def _finalize_packet(self, rudp_packet):
        """
        Convert an packet.RUDPPacket to a string.

        NOTE: It is guaranteed that this method will be called
        exactly once for each outbound packet, so it is the ideal
        place to do pre- or post-processing of any RUDPPacket.
        Consider this when subclassing RUDPConnection.

        Args:
            rudp_packet: A packet.RUDPPacket

        Returns:
            The JSON version of the packet, formatted as a string.
        """
        json_packet = rudp_packet.to_json()
        return json.dumps(json_packet)

    def _do_send_packet(self, seqnum):
        """
        Immediately dispatch packet with given sequence number.

        The packet must have been previously scheduled, that is, it
        should reside in the send window. Upon successful dispatch,
        the timeout timer for this packet is reset and the
        retransmission counter is incremented. If the retries exceed a
        given limit, the connection is considered broken and the
        shutdown sequence is initiated. Finally, the timeout for the
        looping ACK sender is reset.

        Args:
            seqnum: Sequence number of a ScheduledPacket, as an integer.

        Raises:
            KeyError: No such packet exists in the send window; some
                invariant has been violated.
        """
        sch_packet = self._sending_window[seqnum]
        if sch_packet.retries >= constants.MAX_RETRANSMISSIONS:
            self.shutdown()
        else:
            self._proto.send_datagram(sch_packet.rudp_packet, self.relay_addr)
            sch_packet.timeout_cb = REACTOR.callLater(
                sch_packet.timeout,
                self._do_send_packet,
                seqnum
            )
            sch_packet.retries += 1
            self._reset_ack_timeout(constants.KEEP_ALIVE_TIMEOUT)

    def _reset_ack_timeout(self, timeout):
        """
        Reset timeout for bare ACK packet.

        Args:
            timeout: Seconds until a bare ACK packet is sent.
        """
        if self._looping_ack.running:
            self._looping_ack.stop()
            if self.connected:
                self._looping_ack.start(timeout)

    def _clear_sending_window(self):
        """
        Purge send window from scheduled packets.

        Cancel all retransmission timers.
        """
        for sch_packet in self._sending_window.values():
            if sch_packet.timeout_cb.active():
                sch_packet.timeout_cb.cancel()
        self._sending_window.clear()

    def _process_fin_packet(self, rudp_packet):
        """
        Process a received FIN packet.

        Terminate connection after possibly dispatching any
        last messages to handler.

        Args:
            rudp_packet: A packet.RUDPPacket with FIN flag set.
        """
        self.shutdown()

    def _process_casual_packet(self, rudp_packet):
        """
        Process received packet.

        This method can only be called if the connection has been
        established; ignore status of SYN flag.

        Args:
            rudp_packet: A packet.RUDPPacket with FIN flag unset.
        """
        if rudp_packet.ack > 0 and self._sending_window:
            self._retire_packets_with_seqnum_up_to(rudp_packet.ack)

        if rudp_packet.sequence_number > 0:
            self._receive_heap.push(rudp_packet)
            if rudp_packet.sequence_number == self._next_expected_seqnum:
                self._next_expected_seqnum += 1
                self._reset_ack_timeout(constants.BARE_ACK_TIMEOUT)
                self._attempt_enabling_looping_receive()

    def _process_syn_packet(self, rudp_packet):
        """
        Process received SYN packet.

        This method can only be called if the connection has not yet
        been established; thus ignore any payload.

        We use double handshake and consider the connection to the
        remote endpoint established upon either:
            a. Sending a SYNACK packet. If the local host is already
            sending SYN packets, it shall stop doing so and start
            sending SYNACK packets.
            b. Receiving a SYNACK packet ACKing the seqnum of the
            outstanding SYN packet. Careful analysis shows that if
            not both endpoints see the connection as established, at
            least one will keep sending such a packet. Once that
            packet has been successfully received, both endpoints will
            consider the connection as established.

        Once an endpoint considers the connection established, it stops
        processing SYN flags on inbound packets. Past that point, any
        outstanding SYN(ACK) packets should be ACKed by subsequent
        casual packets or bare ACK packets. Failure to do so is
        considered breach of protocol and will lead to connection
        being shutdown.

        Args:
            rudp_packet: A packet.RUDPPacket with SYN flag set.
        """
        if rudp_packet.ack > 0:
            # Prevent crash if malicious node initiates connection
            # with SYNACK message.
            if not self._sending_window:
                return
            lowest_seqnum = tuple(self._sending_window.keys())[0]
            if rudp_packet.ack == lowest_seqnum + 1:
                sch_packet = self._sending_window.pop(lowest_seqnum)
                sch_packet.timeout_cb.cancel()
                self.connected = True
                self._attempt_enabling_looping_send()
        else:
            self._next_expected_seqnum = rudp_packet.sequence_number + 1
            self._clear_sending_window()
            if not self._syn_handle.active():
                self._send_syn()
            self.connected = True

    def _retire_packets_with_seqnum_up_to(self, acknum):
        """
        Remove from send window any ACKed packets.

        Args:
            acknum: Acknowledgement number of next expected
                outbound packet.
        """
        if not self._sending_window:
            return
        lowest_seqnum = tuple(self._sending_window.keys())[0]
        acknum = min(acknum, self._next_sequence_number)
        for seqnum in range(lowest_seqnum, acknum):
            sch_packet = self._sending_window.pop(seqnum)
            sch_packet.timeout_cb.cancel()

        if lowest_seqnum < acknum:
            self._attempt_enabling_looping_send()

    def _pop_received_packet(self):
        """
        Attempt to reconstruct a received packet and process payload.

        If successful, advance ACK number.
        """
        fragments = self._receive_heap.attempt_popping_all_fragments()
        if fragments is None:
            self._attempt_disabling_looping_receive()
        else:
            last_seqnum = fragments[-1].sequence_number
            if self._next_expected_seqnum <= last_seqnum:
                self._next_expected_seqnum = last_seqnum + 1
                self._reset_ack_timeout(constants.BARE_ACK_TIMEOUT)

            payload = ''.join(f.payload for f in fragments)
            self.handler.receive_message(payload)


class Handler(object):

    """
    Abstract base class for handler objects.

    Each RUDPConnection should be linked to one such object.
    """

    __metaclass__ = abc.ABCMeta

    connection = None

    @abc.abstractmethod
    def __init__(self, *args, **kwargs):
        """Create a new Handler."""

    @abc.abstractmethod
    def receive_message(self, message):
        """
        Receive a message from the given connection.

        Args:
            message: The payload of an RUDPPacket, as a string.
        """

    @abc.abstractmethod
    def handle_shutdown(self):
        """Handle connection shutdown."""


class HandlerFactory(object):

    """Abstract base class for handler factory."""

    __metaclass__ = abc.ABCMeta

    @abc.abstractmethod
    def __init__(self, *args, **kwargs):
        """Create a new HandlerFactory."""

    @abc.abstractmethod
    def make_new_handler(self, *args, **kwargs):
        """Create a new handler."""


class RUDPConnectionFactory(object):

    """
    A factory for RUDPConnections.

    Subclass according to need.
    """

    def __init__(self, handler_factory):
        """
        Create a new RUDPConnectionFactory.

        Args:
            handler_factory: An instance of a HandlerFactory,
                providing a `make_new_handler` method.
        """
        self.handler_factory = handler_factory

    def make_new_connection(
        self,
        proto_handle,
        own_addr,
        source_addr,
        relay_addr
    ):
        """
        Create a new RUDPConnection.

        In addition, create a handler and attach the connection to it.
        """
        handler = self.handler_factory.make_new_handler(
            own_addr,
            source_addr,
            relay_addr
        )
        connection = RUDPConnection(
            proto_handle,
            handler,
            own_addr,
            source_addr,
            relay_addr
        )
        handler.connection = connection
