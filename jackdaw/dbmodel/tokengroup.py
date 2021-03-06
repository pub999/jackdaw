from . import Basemodel
import datetime
from sqlalchemy.orm import relationship
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Boolean
import json

class JackDawTokenGroup(Basemodel):
	__tablename__ = 'tokengroup'
	
	id = Column(Integer, primary_key=True)
	ad_id = Column(Integer, ForeignKey('ads.id'))
	ad = relationship("JackDawADInfo", back_populates="group_lookups", lazy = True)
	guid = Column(String, index=True)
	sid = Column(String, index=True)
	member_sid = Column(String, index=True)
	object_type = Column(String, index=True)
	

	@staticmethod
	def from_dict(d):
		t = JackDawTokenGroup()
		t.ad_id = d['ad_id']
		t.guid = d['guid']
		t.sid = d['sid']
		t.member_sid = d['member_sid']
		t.object_type = d['object_type']
		return t

	@staticmethod
	def from_json(x):
		return JackDawTokenGroup.from_dict(json.loads(x))

	def to_dict(self):
		return {
			'ad_id' : self.ad_id,
			'guid' : self.guid,
			'sid' : self.sid,
			'member_sid' : self.member_sid,
			'object_type' : self.object_type
		}

	def to_json(self):
		return json.dumps(self.to_dict())