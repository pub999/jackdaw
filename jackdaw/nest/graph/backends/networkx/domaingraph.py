
from gzip import GzipFile
import pathlib
import multiprocessing as mp
import networkx as nx
from networkx.algorithms.shortest_paths.generic import shortest_path
from bidict import bidict
from jackdaw import logger
from jackdaw.dbmodel.adtrust import JackDawADTrust
from jackdaw.dbmodel.adcomp import JackDawADMachine
from jackdaw.dbmodel.aduser import JackDawADUser
from jackdaw.dbmodel.adgroup import JackDawADGroup
from jackdaw.dbmodel.adinfo import JackDawADInfo
from jackdaw.dbmodel.graphinfo import JackDawGraphInfo
from jackdaw.dbmodel.edge import JackDawEdge
from jackdaw.dbmodel.edgelookup import JackDawEdgeLookup
from jackdaw.dbmodel import windowed_query
from jackdaw.nest.graph.graphdata import GraphData, GraphNode
from jackdaw.nest.graph.construct import GraphConstruct
from jackdaw.wintypes.well_known_sids import get_name_or_sid, get_sid_for_name
import threading
from sqlalchemy import func
from tqdm import tqdm

def short_worker(inqueue, outqueue, graph, dst_sid):
	"""
	Calculates the shortest parth for a given destination node
	This function is "multiprocessed"
	"""
	while True:
		node = inqueue.get()
		if node is None:
			outqueue.put(None)
			return
		try:
			for path in nx.all_shortest_paths(graph, source = node, target = dst_sid):
				outqueue.put(path)
									
		except nx.exception.NetworkXNoPath:
			continue

def short_node_gen(graph, inqueue, dst_sid, procno):
	"""
	Adding all nodes from the graph to the inqueue. At the end it adds terminating object in the amount of proccount.
	This function will be threaded. 
	"""
	for node in graph.nodes:
		if node == dst_sid:
			continue
		inqueue.put(node)
			
	for _ in range(procno):
		inqueue.put(None)


class JackDawDomainGraphNetworkx:
	graph_file_name = 'networkx.csv'
	def __init__(self, session, graph_id):
		self.dbsession = session
		self.graph_id = int(graph_id)
		self.constructs = {}
		self.graph = None
		self.domain_sid = None
		self.domain_id = None
		self.lookup = {}

	def save(self):
		pass
	
	def setup(self):
		gi = self.dbsession.query(JackDawGraphInfo).get(self.graph_id)
		domaininfo = self.dbsession.query(JackDawADInfo).get(gi.ad_id)
		self.domain_sid = domaininfo.objectSid
		self.domain_id = gi.ad_id

	@staticmethod
	def create(dbsession, ad_id, graph_id, graph_dir):
		graph_file = graph_dir.joinpath(JackDawDomainGraphNetworkx.graph_file_name)

		logger.debug('Creating a new graph file: %s' % graph_file)

		## remove this
		fi = dbsession.query(JackDawEdgeLookup.id).filter_by(ad_id = ad_id).filter(JackDawEdgeLookup.oid == 'S-1-5-32-545').first()
		fi = fi[0]
		##

		t2 = dbsession.query(func.count(JackDawEdge.id)).filter_by(graph_id = graph_id).filter(JackDawEdgeLookup.id == JackDawEdge.src).filter(JackDawEdgeLookup.oid != None).scalar()
		q = dbsession.query(JackDawEdge).filter_by(graph_id = graph_id).filter(JackDawEdgeLookup.id == JackDawEdge.src).filter(JackDawEdgeLookup.oid != None)

		with open(graph_file, 'w', newline = '') as f:
			for edge in tqdm(windowed_query(q,JackDawEdge.id, 10000), desc = 'edge', total = t2):
				#if edge.src  == fi:
				#	continue
				#if edge.dst  == fi:
				#	continue
				r = '%s %s\r\n' % (edge.src, edge.dst)
				f.write(r)
		logger.debug('Graph created!')

	@staticmethod
	def load(dbsession, graph_id, graph_cache_dir):
		graph_file = graph_cache_dir.joinpath(JackDawDomainGraphNetworkx.graph_file_name)
		graph = nx.DiGraph()
		g = JackDawDomainGraphNetworkx(dbsession, graph_id)
		g.graph = nx.read_edgelist(str(graph_file), nodetype=int, create_using=graph)
		g.setup()
		logger.debug('Graph loaded to memory')
		return g
		

	def all_shortest_paths(self, src_sid = None, dst_sid = None):
		nv = GraphData()
		
		if not src_sid and not dst_sid:
			raise Exception('Either source or destination MUST be specified')

		elif src_sid is not None and dst_sid is not None:
			src = self.__resolve_sid_to_id(src_sid)
			if src is None:
				raise Exception('SID not found!')

			dst = self.__resolve_sid_to_id(dst_sid)
			if dst is None:
				raise Exception('SID not found!')
			
			for path in nx.all_shortest_paths(self.graph, src, dst):
				self.__result_path_add(nv, path)
		
		elif not src_sid and dst_sid:
			try:
				dst = self.__resolve_sid_to_id(dst_sid)
				if dst is None:
					raise Exception('SID not found!')
				#for each node we calculate the shortest path to the destination node, silently skip the ones who do not have path to dst
				inqueue = mp.Queue()
				outqueue = mp.Queue()
				procno = mp.cpu_count()
				logger.debug('[DST_CALC] Starting processes')
				procs = [mp.Process(target = short_worker, args = (inqueue, outqueue, self.graph, dst_sid)) for i in range(procno)]			
				for proc in procs:
					proc.daemon = True
					proc.start()
				logger.debug('[DST_CALC] Starting generator thread')
				node_gen_th = threading.Thread(target = short_node_gen, args = (self.graph, inqueue, dst_sid, procno))
				node_gen_th.daemon = True
				node_gen_th.start()

				p_cnt = 0
				while True:
					path = outqueue.get()
					if path is None:
						procno -= 1
						logger.debug('[DST_CALC] Proc X - Finished!')
						if procno == 0:
							break
						continue
					self.__result_path_add(nv, path)
					p_cnt += 1

				logger.debug('[DST_CALC] Found %s paths to dst node %s' % (p_cnt, dst_sid))

				logger.debug('[DST_CALC] joining processes')
				for proc in procs:
					proc.join()
				logger.debug('[DST_CALC] Finished!')

			except:
				logger.exception('[DST_CALC]')
			
		else:
			raise Exception('Working on it')
		
		return nv

	def shortest_paths(self, src_sid = None, dst_sid = None):
		nv = GraphData()
		if src_sid is None and dst_sid is None:
			raise Exception('src_sid or dst_sid must be set')
		
		elif src_sid is None and dst_sid is not None:
			dst = self.__resolve_sid_to_id(dst_sid)
			if dst is None:
				raise Exception('SID not found!')

			res = shortest_path(self.graph, target=dst)
			for k in res:
				self.__result_path_add(nv, res[k])



		elif src_sid is not None and dst_sid is not None:
			dst = self.__resolve_sid_to_id(dst_sid)
			if dst is None:
				raise Exception('SID not found!')

			src = self.__resolve_sid_to_id(src_sid)
			if src is None:
				raise Exception('SID not found!')
			
			try:
				res = shortest_path(self.graph, src, dst)
				self.__result_path_add(nv, res)
			except nx.exception.NetworkXNoPath:
				pass
		
		else:
			raise Exception('Not implemented!')

		return nv

	def __result_path_add(self, network, path):
		print(path)
		if path == []:
			return
		path = [i for i in path]
		delete_this = []
		for d, node_id in enumerate(path):
			sid, otype = self.__nodename_to_sid(node_id)
			delete_this.append('%s(%s) -> ' % (sid, otype))
			network.add_node(
				sid, 
				name = self.__sid2cn(sid, otype), 
				node_type = otype,
				domainid = self.domain_id
			)
			network.nodes[sid].set_distance(d)

		print(''.join(delete_this))
		for i in range(len(path) - 1):
			self.__result_edge_add(network, int(path[i]), int(path[i+1]))

	def __result_edge_add(self, network, src_id, dst_id):
		for label in self.__resolv_edge_types(src_id, dst_id):
				try:
					src = self.__nodename_to_sid(src_id)
					dst = self.__nodename_to_sid(dst_id)
					network.add_edge(src[0],dst[0], label=label[0])
					print('%s -> %s [%s]' % (src, dst, label))
				except Exception as e:
					import traceback
					traceback.print_exc()
					print(e)
	
	def __nodename_to_sid(self, node_name):
		node_name = int(node_name)
		if node_name in self.lookup:
			return self.lookup[node_name]
		t = self.dbsession.query(JackDawEdgeLookup).get(node_name) #node_name is the ID of the edgelookup
		self.lookup[node_name] = (t.oid, t.otype)
		return t.oid, t.otype
	
	def __resolv_edge_types(self, src_id, dst_id):
		t = []
		for res in self.dbsession.query(JackDawEdge.label).distinct(JackDawEdge.label).filter_by(graph_id = self.graph_id).filter(JackDawEdge.ad_id == self.domain_id).filter(JackDawEdge.src == src_id).filter(JackDawEdge.dst == dst_id).all():
			t.append(res)
		return t

	def __resolve_sid_to_id(self, sid):
		for res in self.dbsession.query(JackDawEdgeLookup.id).filter_by(ad_id = self.domain_id).filter(JackDawEdgeLookup.oid == sid).first():
			return res
		return None


	def __sid2cn(self, sid, otype):
		if otype == 'user':
			tsid = self.dbsession.query(JackDawADUser.cn).filter(JackDawADUser.objectSid == sid).first()
			if tsid is not None:
				return tsid[0]
		
		elif otype == 'group':
			tsid = self.dbsession.query(JackDawADGroup.cn).filter(JackDawADGroup.objectSid == sid).first()
			if tsid is not None:
				return tsid[0]

		elif otype == 'machine':
			tsid = self.dbsession.query(JackDawADMachine.cn).filter(JackDawADMachine.objectSid == sid).first()
			if tsid is not None:
				return tsid[0]

		elif otype == 'trust':
			tsid = self.dbsession.query(JackDawADTrust.cn).filter(JackDawADTrust.securityIdentifier == sid).first()
			if tsid is not None:
				return tsid[0]
		
		else:
			return None

	def get_domainsids(self):
		pass

	def get_nodes(self):
		pass

	def get_distances_from_node(self):
		pass