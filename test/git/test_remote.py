# test_remote.py
# Copyright (C) 2008, 2009 Michael Trier (mtrier@gmail.com) and contributors
#
# This module is part of GitPython and is released under
# the BSD License: http://www.opensource.org/licenses/bsd-license.php

from test.testlib import *
from git import *
import tempfile
import shutil
import os
import random

# assure we have repeatable results 
random.seed(0)

class TestPushProgress(PushProgress):
	__slots__ = ( "_seen_lines", "_stages_per_op" )
	def __init__(self):
		super(TestPushProgress, self).__init__()
		self._seen_lines = list()
		self._stages_per_op = dict()
		
	def _parse_progress_line(self, line):
		# we may remove the line later if it is dropped
		# Keep it for debugging
		self._seen_lines.append(line)
		super(TestPushProgress, self)._parse_progress_line(line)
		assert len(line) > 1, "line %r too short" % line
		
	def line_dropped(self, line):
		try:
			self._seen_lines.remove(line)
		except ValueError:
			pass
		
	def update(self, op_code, cur_count, max_count=None, message=''):
		# check each stage only comes once
		op_id = op_code & self.OP_MASK
		assert op_id in (self.COUNTING, self.COMPRESSING, self.WRITING)
		
		self._stages_per_op.setdefault(op_id, 0)
		self._stages_per_op[ op_id ] = self._stages_per_op[ op_id ] | (op_code & self.STAGE_MASK)
		
		if op_code & (self.WRITING|self.END) == (self.WRITING|self.END):
			assert message
		# END check we get message 
		
	def make_assertion(self):
		if not self._seen_lines:
			return 
		
		# sometimes objects are not compressed which is okay
		assert len(self._seen_ops) in (2,3)
		assert self._stages_per_op
		
		# must have seen all stages
		for op, stages in self._stages_per_op.items():
			assert stages & self.STAGE_MASK == self.STAGE_MASK
		# END for each op/stage

class TestRemote(TestBase):
	
	def _print_fetchhead(self, repo):
		fp = open(os.path.join(repo.path, "FETCH_HEAD"))
		print fp.read()
		fp.close()
		
		
	def _test_fetch_result(self, results, remote):
		# self._print_fetchhead(remote.repo)
		assert len(results) > 0 and isinstance(results[0], FetchInfo)
		for info in results:
			if isinstance(info.ref, Reference):
				assert info.flags != 0
			# END reference type flags handling 
			assert isinstance(info.ref, (SymbolicReference, Reference))
			if info.flags & info.FORCED_UPDATE:
				assert isinstance(info.commit_before_forced_update, Commit)
			else:
				assert info.commit_before_forced_update is None
			# END forced update checking  
		# END for each info
		
	def _test_push_result(self, results, remote):
		assert len(results) > 0 and isinstance(results[0], PushInfo)
		for info in results:
			assert info.flags
			if info.old_commit is not None:
				assert isinstance(info.old_commit, Commit)
			if info.flags & info.ERROR:
				has_one = False
				for bitflag in (info.REJECTED, info.REMOTE_REJECTED, info.REMOTE_FAILURE):
					has_one |= bool(info.flags & bitflag)
				# END for each bitflag
				assert has_one
			else:
				# there must be a remote commit
				if info.flags & info.DELETED == 0: 
					assert isinstance(info.local_ref, Reference)
				else:
					assert info.local_ref is None
				assert type(info.remote_ref) in (TagReference, RemoteReference)
			# END error checking
		# END for each info 
		
		
	def _test_fetch_info(self, repo):
		self.failUnlessRaises(ValueError, FetchInfo._from_line, repo, "nonsense", '')
		self.failUnlessRaises(ValueError, FetchInfo._from_line, repo, "? [up to date]      0.1.7RC    -> origin/0.1.7RC", '')
		
	def _commit_random_file(self, repo):
		#Create a file with a random name and random data and commit it to  repo.
		# Return the commited absolute file path
		index = repo.index
		new_file = self._make_file(os.path.basename(tempfile.mktemp()),str(random.random()), repo)
		index.add([new_file])
		index.commit("Committing %s" % new_file)
		return new_file
		
	def _test_fetch(self,remote, rw_repo, remote_repo):
		# specialized fetch testing to de-clutter the main test
		self._test_fetch_info(rw_repo)
		
		def fetch_and_test(remote, **kwargs):
			res = remote.fetch(**kwargs)
			self._test_fetch_result(res, remote)
			return res
		# END fetch and check
		
		def get_info(res, remote, name):
			return res["%s/%s"%(remote,name)]
		
		# put remote head to master as it is garantueed to exist
		remote_repo.head.reference = remote_repo.heads.master
		
		res = fetch_and_test(remote)
		# all uptodate
		for info in res:
			assert info.flags & info.HEAD_UPTODATE
		
		# rewind remote head to trigger rejection
		# index must be false as remote is a bare repo
		rhead = remote_repo.head
		remote_commit = rhead.commit
		rhead.reset("HEAD~2", index=False)
		res = fetch_and_test(remote)
		mkey = "%s/%s"%(remote,'master')
		master_info = res[mkey]
		assert master_info.flags & FetchInfo.FORCED_UPDATE and master_info.note is not None
		
		# normal fast forward - set head back to previous one
		rhead.commit = remote_commit
		res = fetch_and_test(remote)
		assert res[mkey].flags & FetchInfo.FAST_FORWARD
		
		# new remote branch
		new_remote_branch = Head.create(remote_repo, "new_branch")
		res = fetch_and_test(remote)
		new_branch_info = get_info(res, remote, new_remote_branch)
		assert new_branch_info.flags & FetchInfo.NEW_HEAD
		
		# remote branch rename ( causes creation of a new one locally )
		new_remote_branch.rename("other_branch_name")
		res = fetch_and_test(remote)
		other_branch_info = get_info(res, remote, new_remote_branch)
		assert other_branch_info.ref.commit == new_branch_info.ref.commit
		
		# remove new branch
		Head.delete(new_remote_branch.repo, new_remote_branch)
		res = fetch_and_test(remote)
		# deleted remote will not be fetched
		self.failUnlessRaises(IndexError, get_info, res, remote, new_remote_branch)
		
		# prune stale tracking branches
		stale_refs = remote.stale_refs
		assert len(stale_refs) == 2 and isinstance(stale_refs[0], RemoteReference)
		RemoteReference.delete(rw_repo, *stale_refs)
		
		# test single branch fetch with refspec including target remote
		res = fetch_and_test(remote, refspec="master:refs/remotes/%s/master"%remote)
		assert len(res) == 1 and get_info(res, remote, 'master')
		
		# ... with respec and no target
		res = fetch_and_test(remote, refspec='master')
		assert len(res) == 1
		
		# add new tag reference
		rtag = TagReference.create(remote_repo, "1.0-RV_hello.there")
		res = fetch_and_test(remote, tags=True)
		tinfo = res[str(rtag)]
		assert isinstance(tinfo.ref, TagReference) and tinfo.ref.commit == rtag.commit
		assert tinfo.flags & tinfo.NEW_TAG
		
		# adjust tag commit
		rtag.object = rhead.commit.parents[0].parents[0]
		res = fetch_and_test(remote, tags=True)
		tinfo = res[str(rtag)]
		assert tinfo.commit == rtag.commit
		assert tinfo.flags & tinfo.TAG_UPDATE
		
		# delete remote tag - local one will stay
		TagReference.delete(remote_repo, rtag)
		res = fetch_and_test(remote, tags=True)
		self.failUnlessRaises(IndexError, get_info, res, remote, str(rtag))
		
		# provoke to receive actual objects to see what kind of output we have to 
		# expect. For that we need a remote transport protocol
		# Create a new UN-shared repo and fetch into it after we pushed a change
		# to the shared repo
		other_repo_dir = tempfile.mktemp("other_repo")
		# must clone with a local path for the repo implementation not to freak out
		# as it wants local paths only ( which I can understand )
		other_repo = remote_repo.clone(other_repo_dir, shared=False)
		remote_repo_url = "git://localhost%s"%remote_repo.path
		
		# put origin to git-url
		other_origin = other_repo.remotes.origin 
		other_origin.config_writer.set("url", remote_repo_url)
		# it automatically creates alternates as remote_repo is shared as well.
		# It will use the transport though and ignore alternates when fetching
		# assert not other_repo.alternates	# this would fail
		
		# assure we are in the right state
		rw_repo.head.reset(remote.refs.master, working_tree=True)
		try:
			self._commit_random_file(rw_repo)
			remote.push(rw_repo.head.reference)
			
			# here I would expect to see remote-information about packing 
			# objects and so on. Unfortunately, this does not happen 
			# if we are redirecting the output - git explicitly checks for this
			# and only provides progress information to ttys
			res = fetch_and_test(other_origin)
		finally:
			shutil.rmtree(other_repo_dir)
		# END test and cleanup
		
	def _test_push_and_pull(self,remote, rw_repo, remote_repo):
		# push our changes
		lhead = rw_repo.head
		lindex = rw_repo.index
		# assure we are on master and it is checked out where the remote is
		lhead.reference = rw_repo.heads.master
		lhead.reset(remote.refs.master, working_tree=True)
		
		# push without spec should fail ( without further configuration )
		# well, works nicely
		# self.failUnlessRaises(GitCommandError, remote.push)
		
		# simple file push
		self._commit_random_file(rw_repo)
		progress = TestPushProgress()
		res = remote.push(lhead.reference, progress)
		assert isinstance(res, IterableList)
		self._test_push_result(res, remote)
		progress.make_assertion()
		
		# rejected - undo last commit
		lhead.reset("HEAD~1")
		res = remote.push(lhead.reference)
		assert res[0].flags & PushInfo.ERROR 
		assert res[0].flags & PushInfo.REJECTED
		self._test_push_result(res, remote)
		
		# force rejected pull
		res = remote.push('+%s' % lhead.reference)
		assert res[0].flags & PushInfo.ERROR == 0 
		assert res[0].flags & PushInfo.FORCED_UPDATE
		self._test_push_result(res, remote)
		
		# invalid refspec
		res = remote.push("hellothere")
		assert len(res) == 0
		
		# push new tags 
		progress = TestPushProgress()
		to_be_updated = "my_tag.1.0RV"
		new_tag = TagReference.create(rw_repo, to_be_updated)
		other_tag = TagReference.create(rw_repo, "my_obj_tag.2.1aRV", message="my message")
		res = remote.push(progress=progress, tags=True)
		assert res[-1].flags & PushInfo.NEW_TAG
		progress.make_assertion()
		self._test_push_result(res, remote)
		
		# update push new tags
		# Rejection is default
		new_tag = TagReference.create(rw_repo, to_be_updated, ref='HEAD~1', force=True)
		res = remote.push(tags=True)
		self._test_push_result(res, remote)
		assert res[-1].flags & PushInfo.REJECTED and res[-1].flags & PushInfo.ERROR
		
		# push force this tag
		res = remote.push("+%s" % new_tag.path)
		assert res[-1].flags & PushInfo.ERROR == 0 and res[-1].flags & PushInfo.FORCED_UPDATE
		
		# delete tag - have to do it using refspec
		res = remote.push(":%s" % new_tag.path)
		self._test_push_result(res, remote)
		assert res[0].flags & PushInfo.DELETED
		
		self.fail("test --all")
		
		# push new branch
		
		# delete new branch
		
		# pull is essentially a fetch + merge, hence we just do a light 
		# test here, leave the reset to the actual merge testing
		# fails as we did not specify a branch and there is no configuration for it
		self.failUnlessRaises(GitCommandError, remote.pull)
		remote.pull('master')
	
	@with_rw_and_rw_remote_repo('0.1.6')
	def test_base(self, rw_repo, remote_repo):
		num_remotes = 0
		remote_set = set()
		ran_fetch_test = False
		
		for remote in rw_repo.remotes:
			num_remotes += 1
			assert remote == remote
			assert str(remote) != repr(remote)
			remote_set.add(remote)
			remote_set.add(remote)	# should already exist
			
			# REFS 
			refs = remote.refs
			assert refs
			for ref in refs:
				assert ref.remote_name == remote.name
				assert ref.remote_head
			# END for each ref
			
			# OPTIONS
			# cannot use 'fetch' key anymore as it is now a method
			for opt in ("url", ):
				val = getattr(remote, opt)
				reader = remote.config_reader
				assert reader.get(opt) == val
				
				# unable to write with a reader
				self.failUnlessRaises(IOError, reader.set, opt, "test")
				
				# change value
				writer = remote.config_writer
				new_val = "myval"
				writer.set(opt, new_val)
				assert writer.get(opt) == new_val
				writer.set(opt, val)
				assert writer.get(opt) == val
				del(writer)
				assert getattr(remote, opt) == val
			# END for each default option key 
			
			# RENAME 
			other_name = "totally_other_name"
			prev_name = remote.name
			assert remote.rename(other_name) == remote
			assert prev_name != remote.name
			# multiple times
			for time in range(2):
				assert remote.rename(prev_name).name == prev_name
			# END for each rename ( back to prev_name )
			
			# PUSH/PULL TESTING
			self._test_push_and_pull(remote, rw_repo, remote_repo)
			
			# FETCH TESTING
			# Only for remotes - local cases are the same or less complicated 
			# as additional progress information will never be emitted
			if remote.name == "daemon_origin":
				self._test_fetch(remote, rw_repo, remote_repo)
				ran_fetch_test = True
			# END fetch test  
			
			remote.update()
		# END for each remote
		
		assert ran_fetch_test
		assert num_remotes
		assert num_remotes == len(remote_set)
		
		origin = rw_repo.remote('origin')
		assert origin == rw_repo.remotes.origin
		
	@with_bare_rw_repo
	def test_creation_and_removal(self, bare_rw_repo):
		new_name = "test_new_one"
		arg_list = (new_name, "git@server:hello.git")
		remote = Remote.create(bare_rw_repo, *arg_list )
		assert remote.name == "test_new_one"
		assert remote in bare_rw_repo.remotes
		
		# create same one again
		self.failUnlessRaises(GitCommandError, Remote.create, bare_rw_repo, *arg_list)
		
		Remote.remove(bare_rw_repo, new_name)
		
		for remote in bare_rw_repo.remotes:
			if remote.name == new_name:
				raise AssertionError("Remote removal failed")
			# END if deleted remote matches existing remote's name
		# END for each remote
		
		
	
