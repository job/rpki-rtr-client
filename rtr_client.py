#!/usr/bin/env python3

import sys
import os
import getopt
import socket
import select
import time
import json

from rtr_protocol import rfc8210router

#
# rtr protocol - port 8282 - clear text - Cisco, Juniper
# rtr_protocol - port 8283 - ssh - Juniper
# rtr_protocol - port 8284 - tls - ?
#

class Connect(object):
	rtr_host = 'rtr.rpki.cloudflare.com'
	rtr_port = 8282
	fd = None
	connect_timeout = 5 # this is about the socket connect timeout and not data timeout

	def __init__(self, host=None, port=None):
		if host:
			self.rtr_host = host
		if port:
			self.rtr_port = port
		self.fd = self._connect()

	def _sleep(self, n):
		# simple back off for failed connect
		time.sleep(n)

	def _connect(self):
		for ii in [1,2,4,8,16,32]:
			try:
				fd = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
				fd.settimeout(self.connect_timeout)
				fd.connect((self.rtr_host, self.rtr_port))
				return fd
			except KeyboardInterrupt:
				sys.stderr.write('socket connection: ^C\n')
				sys.stderr.flush()
				exit(1)
			except socket.timeout:
				sys.stderr.write('socket connection: Timeout\n ')
				sys.stderr.flush()
				self._sleep(ii)
				continue
			except socket.error:
				sys.stderr.write('socket connection: Error %s\n ' % (socket.error))
				sys.stderr.flush()
				self._sleep(ii)
				continue
		return None

class Process(object):
	class Buffer(object):
		def __init__(self):
			self.last_buffer = None

		def clear(self):
			if self.last_buffer:
				self.last_buffer = None
		def read(self):
			b = self.last_buffer
			if self.last_buffer:
				self.last_buffer = None
			return b

		def write(self, b):
			self.last_buffer = b
			# sys.stderr.write('LEFTOVER(%d)\n' % len(self.last_buffer))
			# sys.stderr.flush()

	def __init__(self):
		self.buf = self.Buffer()
		pass

	def do_hunk(self, rtr_session, v):
		# sys.stderr.write('(%d)' % len(v))
		# sys.stderr.flush()
		if not v or len(v) == 0:
			# END OF FILE
			return False

		b = self.buf.read()
		if b:
			v = b + v
		data_length = len(v)
		data_left = rtr_session.process(v)
		if data_left > 0:
			self.buf.write(v[data_length - data_left:])
		return True

	def clear(self):
		self.buf.clear()

def data_directory():
	try:
		os.mkdir('data')
	except FileExistsError:
		pass

def dump_routes(rtr_session, serial):
	# dump present routes into file based on serial number
	routes = rtr_session.routes()
	if len(routes['announce']) > 0 or len(routes['withdraw']) > 0:
		data_directory()
		j = {'serial': serial, 'routes': routes}
		with open('data/routes.%08d.json' % (serial), 'w') as fd:
			fd.write(json.dumps(j))
		rtr_session.clear_routes()
		sys.stderr.write('\nDUMP ROUTES: serial=%d announce=%d/withdraw=%d\n' % (serial, len(routes['announce']), len(routes['withdraw'])))
		sys.stderr.flush()

def rtr_client(host=None, port=None, serial=None, timeout=None, dump=False, debug=False):
	rtr_session = rfc8210router(serial=serial, debug=debug)

	if dump:
		data_directory()
		dump_fd = open('data/__________-raw-data.bin', 'w')

	p = Process()

	cache_fd = None
	while True:
		if not cache_fd:
			p.clear()
			sys.stderr.write('RECONNECT\n')
			sys.stderr.flush()
			connection = Connect(host, port)
			cache_fd = connection.fd

		if not cache_fd:
			sys.stderr.write('\nNO NETWORK CONNECTION\n')
			sys.stderr.flush()
			exit(1)	

		if serial == None or serial == 0:
			packet = rtr_session.reset_query()
			serial = 0
		else:
			packet = rtr_session.serial_query(serial)
		sys.stderr.write('+')
		sys.stderr.flush()
		cache_fd.send(packet)
		rtr_session.process(packet)

		while True:

			# At every oppertunity, see if we have a new serial number
			new_serial = rtr_session.cache_serial_number()
			if new_serial != serial:
				# dump present routes into file based on serial number
				dump_routes(rtr_session, new_serial)
				# update serial number
				sys.stderr.write('NEW SERIAL %d->%d\n' % (serial, new_serial))
				sys.stderr.flush()
				serial = new_serial

			try:
				ready = select.select([cache_fd], [], [], timeout)
			except KeyboardInterrupt:
				sys.stderr.write('\nselect wait: ^C\n')
				sys.stderr.flush()
				exit(1)
			except Exception as e:
				sys.stderr.write('\nselect wait: %s\n' % (e))
				sys.stderr.flush()
				break

			if not ready[0]:
				# Timeout
				sys.stderr.write('T')
				sys.stderr.flush()

				if rtr_session.time_remaining():
					sys.stderr.write('-')
					sys.stderr.flush()
					continue

				## sys.stderr.write('\n')
				## sys.stderr.flush()
				# timed out - go ask for more data!
				packet = rtr_session.serial_query()
				rtr_session.process(packet)
				cache_fd.send(packet)
				continue

			try:
				sys.stderr.write('.')
				sys.stderr.flush()
				v = cache_fd.recv(64*1024)
			except Exception as e:
				sys.stderr.write('recv: %s\n' % (e))
				sys.stderr.flush()
				v = None
				cache_fd.close()
				cache_fd = None
				break

			if dump:
				# save raw data away
				dump_fd.buffer.write(v)
				dump_fd.flush()

			if not p.do_hunk(rtr_session, v):
				break
	return

def doit(args=None):
	debug = False
	dump = False
	host = None
	port = None
	serial = None
	timeout = 30 # thirty seconds for some random reason

	usage = ('usage: rtr_client '
		 + '[-H|--help] '
		 + '[-v|--verbose] '
		 + '[-h|--host] hostname '
		 + '[-p|--port] portnumber '
		 + '[-s|--serial] serialnumber '
		 + '[-t|--timeout] seconds '
		 + '[-d|--dump] '
		 )

	try:
		opts, args = getopt.getopt(args,
					   'Hvh:p:s:t:d',
					   [
					   	'help',
					   	'version',
					   	'host=', 'port=',
						'serial=',
						'timeout=',
						'debug'
					   ])
	except getopt.GetoptError:
		exit(usage)
	for opt, arg in opts:
		if opt in ('-H', '--help'):
			exit(usage)
		elif opt in ('-v', '--verbose'):
			debug = True
		elif opt in ('-h', '--host'):
			host = arg
		elif opt in ('-p', '--port'):
			port = int(arg)
		elif opt in ('-s', '--serial'):
			serial = arg
		elif opt in ('-t', '--timeout'):
			timeout = int(arg)
		elif opt in ('-d', '--dump'):
			dump = True

	rtr_client(host=host, port=port, serial=serial, timeout=timeout, dump=dump, debug=debug)
	exit(0)

def main(args=None):
	if args is None:
		args = sys.argv[1:]
	doit(args)

if __name__ == '__main__':
	main()
