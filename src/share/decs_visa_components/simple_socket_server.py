"""
A basing implemention of a TCP/IP socket server
"""
import socket
import queue

from decs_visa_tools.base_logger import logger

# response read delimiter
from decs_visa_tools.decs_visa_settings import READ_DELIM
# response write delimiter
from decs_visa_tools.decs_visa_settings import WRITE_DELIM
# shutdown message
from decs_visa_tools.decs_visa_settings import SHUTDOWN

def parse_data(data: str) -> str:
    """
    Utility function to strip off the delimiter
    from messages recieved by the server 
    """
    return data.removesuffix(READ_DELIM)

def format_message(resp: str) -> bytes:
    """
    Utility function to add a delimiter and
    utf-8 encode a WAMP response for sending
    """
    msg = str(resp)+WRITE_DELIM
    return msg.encode('utf-8')

def simple_server(interface: str, server_port: int, q: queue.Queue, r: queue.Queue) -> None:
    """
    The simple server
    """
    server_port = int(server_port)
    can_run = True
    simple_socket_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # This allows the OS to rebind to the same address without any delay
    # This allows DECS<->VISA to be re-run if a WAMP error caused a SHUTDOWN
    # the likelyhood of a valid message being in transit on the network causing
    # unexpected behaviour seems low - alternativly change server_port on each run
    simple_socket_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        # Interface that the simple_socket_server will accept connections from.
        # Can be restricted to 'localhost' (i.e. this machine) as an added security
        # feature, or change to "" if you want to accept general network traffic.
        simple_socket_server.bind((interface, server_port))
        logger.info("Server listening: %s:%s", interface, str(server_port))
        simple_socket_server.settimeout(1)
        simple_socket_server.listen(0)
    except Exception as e:
        # didn't manage to open the socket
        can_run = False
        q.put(SHUTDOWN)
        logger.info('Unable to bind socket server: %s', e)

    while can_run:
        try:
            conn, addr = simple_socket_server.accept()
        except socket.timeout:
            # no connection request yet
            # check if WAMP session has died before socket connection established
            logger.debug("Waiting for socket connection")
            try:
                resp = r.get_nowait()
                # Anything in here must be bad
                logger.debug("WAMP session closed before socket server connection established")
                can_run = False
            except queue.Empty:
                # Nothing bad seems to have happened yet...
                pass
        else:
            with conn:
                logger.info("Server connection: %s", str(addr))
                while can_run:
                    data: str | None
                    data = ""
                    # Read one command at a time...
                    while not data.endswith(READ_DELIM):
                        char_byte = conn.recv(1)
                        if not char_byte:
                            logger.info("Client disconnected")
                            data = None
                            break
                        char = char_byte.decode('utf-8')
                        data += char
                    if data is None:
                        break
                    msg = parse_data(data)
                    logger.debug("Socket server received: \"%s\"", msg)
                    # Add message to the WAMP queue for processing
                    q.put(msg)
                    if msg == SHUTDOWN: # shutdown request from user
                        can_run = False
                        break
                    # This will now block until WAMP call is processed...
                    resp = r.get(block=True, timeout=None)
                    logger.debug("Socket server Sending: %s", (str(resp)))
                    # Return the response to the client
                    conn.sendall(format_message(resp)) # - could throw?
                    if resp == SHUTDOWN: # shutdown request as a result of a WAMP error
                        can_run = False
                        break

    logger.info("Socket server shutting down")
    simple_socket_server.close()
