
import os
import gzip
import json
import datetime
import asyncio

from jackdaw.gatherer.progress import *
from jackdaw.gatherer.ldap.agent.common import *
from jackdaw.gatherer.ldap.agent.agent import LDAPGathererAgent
from jackdaw.dbmodel.graphinfo import JackDawGraphInfo
from jackdaw.dbmodel.adgroup import JackDawADGroup
from jackdaw.dbmodel.adinfo import JackDawADInfo
from jackdaw.dbmodel.aduser import JackDawADUser
from jackdaw.dbmodel.adcomp import JackDawADMachine
from jackdaw.dbmodel.adou import JackDawADOU
from jackdaw.dbmodel.adgpo import JackDawADGPO
from jackdaw.dbmodel.adtrust import JackDawADTrust
from jackdaw.dbmodel.tokengroup import JackDawTokenGroup
from jackdaw.dbmodel import get_session
from jackdaw.dbmodel.edge import JackDawEdge
from jackdaw.dbmodel.edgelookup import JackDawEdgeLookup
from jackdaw.dbmodel import windowed_query
from jackdaw import logger

from tqdm import tqdm
from sqlalchemy import func


class MembershipCollector:
	def __init__(self, session, ldap_mgr, ad_id = None, agent_cnt = None, progress_queue = None, show_progress = True, graph_id = None, resumption = False, members_target_file_handle = None, store_to_db = True):
		self.session = session
		self.ldap_mgr = ldap_mgr
		self.agent_cnt = agent_cnt
		self.ad_id = ad_id
		self.graph_id = graph_id
		self.domain_name = None
		self.resumption = resumption
		self.members_target_file_handle = members_target_file_handle
		
		self.progress_queue = progress_queue
		self.show_progress = show_progress

		if self.agent_cnt is None:
			self.agent_cnt = min(len(os.sched_getaffinity(0)), 8)

		self.member_finish_ctr = 0
		self.agent_in_q = None
		self.agent_out_q = None
		self.total_targets = 0
		self.total_members_to_poll = 0
		self.progress_last_updated = datetime.datetime.utcnow()
		self.agents = []
		self.progress_step_size = 1000
		self.lookup = {}
		self.store_to_db = store_to_db

	def sid_to_id_lookup(self, sid, ad_id, object_type):
		if sid in self.lookup:
			return self.lookup[sid]

		src_id = self.session.query(JackDawEdgeLookup.id).filter_by(oid = sid).filter(JackDawEdgeLookup.ad_id == ad_id).first()
		if src_id is None:
			t = JackDawEdgeLookup(ad_id, sid, object_type)
			self.session.add(t)
			self.session.commit()
			self.session.refresh(t)
			src_id = t.id
			self.lookup[sid] = src_id
		else:
			src_id = src_id[0]
			self.lookup[sid] = src_id
		return src_id

	async def resumption_target_gen_member(self,q, id_filed, obj_type, jobtype):
		for dn, sid, guid in windowed_query(q, id_filed, 10, is_single_entity = False):
			#print(dn)
			data = {
				'dn' : dn,
				'sid' : sid,
				'guid' : guid,
				'object_type' : obj_type
			}
			self.members_target_file_handle.write(json.dumps(data).encode() + b'\r\n')
			self.total_members_to_poll += 1

	async def generate_member_targets(self):
		try:
			subq = self.session.query(JackDawEdgeLookup.oid).filter_by(ad_id = self.ad_id).filter(JackDawEdgeLookup.id == JackDawEdge.src).filter(JackDawEdge.label == 'member').filter(JackDawEdge.ad_id == self.ad_id)
			q = self.session.query(JackDawADUser.dn, JackDawADUser.objectSid, JackDawADUser.objectGUID)\
				.filter_by(ad_id = self.ad_id)\
				.filter(~JackDawADUser.objectSid.in_(subq))
			await self.resumption_target_gen_member(q, JackDawADUser.id, 'user', LDAPAgentCommand.MEMBERSHIPS)
			q = self.session.query(JackDawADMachine.dn, JackDawADMachine.objectSid, JackDawADMachine.objectGUID)\
				.filter_by(ad_id = self.ad_id)\
				.filter(~JackDawADMachine.objectSid.in_(subq))
			await self.resumption_target_gen_member(q, JackDawADMachine.id, 'machine', LDAPAgentCommand.MEMBERSHIPS)
			q = self.session.query(JackDawADGroup.dn, JackDawADGroup.objectSid, JackDawADGroup.objectGUID)\
				.filter_by(ad_id = self.ad_id)\
				.filter(~JackDawADGroup.objectSid.in_(subq))
			await self.resumption_target_gen_member(q, JackDawADGroup.id, 'group', LDAPAgentCommand.MEMBERSHIPS)
			
		except Exception as e:
			logger.exception('generate_member_targets')
			

	async def stop_memberships_collection(self):
		for _ in range(len(self.agents)):
			await self.agent_in_q.put(None)

		for agent in self.agents:
			agent.cancel()
		
		
		if self.show_progress is True:
			self.member_progress.refresh()
			self.member_progress.disable = True

		if self.progress_queue is not None:
			msg = GathererProgress()
			msg.type = GathererProgressType.MEMBERS
			msg.msg_type = MSGTYPE.FINISHED
			msg.adid = self.ad_id
			msg.domain_name = self.domain_name
			await self.progress_queue.put(msg)
		
		if self.store_to_db is True:
			await self.store_file_data()

	async def store_file_data(self):
		try:
			if self.progress_queue is not None:
				msg = GathererProgress()
				msg.type = GathererProgressType.MEMBERSUPLOAD
				msg.msg_type = MSGTYPE.STARTED
				msg.adid = self.ad_id
				msg.domain_name = self.domain_name
				await self.progress_queue.put(msg)

			if self.show_progress is True:
				self.upload_pbar = tqdm(desc='Uploading memberships to DB', total=self.member_finish_ctr)

			self.token_file.close()
			cnt = 0
			with gzip.GzipFile(self.token_file_path, 'r') as f:
				for line in f:
					sd = JackDawTokenGroup.from_json(line.strip())
					src_id = self.sid_to_id_lookup(sd.sid, sd.ad_id, sd.object_type)
					dst_id = self.sid_to_id_lookup(sd.member_sid, sd.ad_id, sd.object_type)

					edge = JackDawEdge(sd.ad_id, self.graph_id, src_id, dst_id, 'member')

					self.session.add(edge)
					cnt += 1
					if cnt % 1000 == 0:
						self.session.commit()

					if self.show_progress is True:
						self.upload_pbar.update()
					
					if cnt % self.progress_step_size == 0 and self.progress_queue is not None:
						now = datetime.datetime.utcnow()
						td = (now - self.progress_last_updated).total_seconds()
						self.progress_last_updated = now
						msg = GathererProgress()
						msg.type = GathererProgressType.MEMBERSUPLOAD
						msg.msg_type = MSGTYPE.PROGRESS
						msg.adid = self.ad_id
						msg.domain_name = self.domain_name
						msg.total = self.member_finish_ctr
						msg.total_finished = cnt
						msg.speed = str(self.progress_step_size // td)
						msg.step_size = self.progress_step_size
						await self.progress_queue.put(msg)
						await asyncio.sleep(0)
					

			self.session.commit()
			if self.progress_queue is not None:
				msg = GathererProgress()
				msg.type = GathererProgressType.MEMBERSUPLOAD
				msg.msg_type = MSGTYPE.FINISHED
				msg.adid = self.ad_id
				msg.domain_name = self.domain_name
				await self.progress_queue.put(msg)


			return True, None
			
		except Exception as e:
			logger.exception('Error while uploading memberships from file to DB')
			if self.progress_queue is not None:
				msg = GathererProgress()
				msg.type = GathererProgressType.MEMBERSUPLOAD
				msg.msg_type = MSGTYPE.ERROR
				msg.adid = self.ad_id
				msg.domain_name = self.domain_name
				msg.error = e
				await self.progress_queue.put(msg)

			return None, e
		finally:
			if self.token_file_path is not None:
				try:
					os.remove(self.token_file_path)
				except:
					pass

	async def prepare_targets(self):
		try:
			if self.resumption is True:
				self.total_targets = 1
				if self.members_target_file_handle is not None:
					raise Exception('Resumption doesnt use the target file handle!') 
				
				self.members_target_file_handle = gzip.GzipFile('member_targets.gz','wb')
				await self.generate_member_targets()

			else:
				self.members_target_file_handle.seek(0,0)
				for line in self.members_target_file_handle:
					self.total_members_to_poll += 1

			return True, None
		
		except Exception as err:
			logger.exception('prep targets')
			return False, err

	async def start_jobs(self):
		self.members_target_file_handle.seek(0,0)
		for line in self.members_target_file_handle:
				line = line.strip()
				line = line.decode()
				data = json.loads(line)
				job = LDAPAgentJob(LDAPAgentCommand.MEMBERSHIPS, data)
				await self.agent_in_q.put(job)

	async def run(self):
		try:

			qs = self.agent_cnt
			self.agent_in_q = asyncio.Queue(qs)
			self.agent_out_q = asyncio.Queue(qs)

			self.token_file_path = 'token_' + datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S") + '.gzip'
			self.token_file = gzip.GzipFile(self.token_file_path, 'w')	

			logger.debug('Polling members')
			_, res = await self.prepare_targets()
			if res is not None:
				raise res
			
			for _ in range(self.agent_cnt):
				agent = LDAPGathererAgent(self.ldap_mgr, self.agent_in_q, self.agent_out_q)
				self.agents.append(asyncio.create_task(agent.arun()))

			
			asyncio.create_task(self.start_jobs())
			if self.progress_queue is None:
				self.member_progress = tqdm(desc='Collecting members', total=self.total_members_to_poll, position=0, leave=True)
			else:
				msg = GathererProgress()
				msg.type = GathererProgressType.MEMBERS
				msg.msg_type = MSGTYPE.STARTED
				msg.adid = self.ad_id
				msg.domain_name = self.domain_name
				await self.progress_queue.put(msg)

			acnt = self.total_members_to_poll
			while acnt > 0:
				try:
					res = await self.agent_out_q.get()
					res_type, res = res
						
					if res_type == LDAPAgentCommand.MEMBERSHIP:
						self.member_finish_ctr += 1
						res.ad_id = self.ad_id
						res.graph_id = self.graph_id
						self.token_file.write(res.to_json().encode() + b'\r\n')
					
					elif res_type == LDAPAgentCommand.MEMBERSHIP_FINISHED:
						if self.progress_queue is None:
							self.member_progress.update()
						
						else:
							if acnt % self.progress_step_size == 0:
								now = datetime.datetime.utcnow()
								td = (now - self.progress_last_updated).total_seconds()
								self.progress_last_updated = now
								msg = GathererProgress()
								msg.type = GathererProgressType.MEMBERS
								msg.msg_type = MSGTYPE.PROGRESS
								msg.adid = self.ad_id
								msg.domain_name = self.domain_name
								msg.total = self.total_members_to_poll
								msg.total_finished = self.total_members_to_poll - acnt
								msg.speed = str(self.progress_step_size // td)
								msg.step_size = self.progress_step_size
								await self.progress_queue.put(msg)
						acnt -= 1

					elif res_type == LDAPAgentCommand.EXCEPTION:
						logger.warning(str(res))
						
				except Exception as e:
					logger.exception('Members enumeration error!')
					raise e
			
			await self.stop_memberships_collection()
			return True, None
		except Exception as e:
			logger.exception('Members enumeration error main!')
			await self.stop_memberships_collection()
			return False, e
