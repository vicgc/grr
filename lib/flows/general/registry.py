#!/usr/bin/env python
"""Gather information from the registry on windows."""

import re
import stat

from grr.lib import aff4
from grr.lib import artifact
from grr.lib import artifact_lib
from grr.lib import flow
from grr.lib import rdfvalue
from grr.lib import utils


# TODO(user): replace this flow with chained artifacts once the capability
# exists.
class CollectRunKeyBinaries(flow.GRRFlow):
  """Collect the binaries used by Run and RunOnce keys on the system.

  We use the RunKeys artifact to get RunKey command strings for all users and
  System. This flow guesses file paths from the strings, expands any
  windows system environment variables, and attempts to retrieve the files.
  """
  category = "/Registry/"
  behaviours = flow.GRRFlow.behaviours + "BASIC"

  @flow.StateHandler(next_state="ParseRunKeys")
  def Start(self):
    """Get runkeys via the ArtifactCollectorFlow."""
    self.CallFlow("ArtifactCollectorFlow", artifact_list=["WindowsRunKeys"],
                  use_tsk=True, store_results_in_aff4=False,
                  next_state="ParseRunKeys")

  def _IsExecutableExtension(self, path):
    return path.endswith(("exe", "com", "bat", "dll", "msi", "sys", "scr",
                          "pif"))

  @flow.StateHandler(next_state="Done")
  def ParseRunKeys(self, responses):
    """Get filenames from the RunKeys and download the files."""
    filenames = []
    client = aff4.FACTORY.Open(self.client_id, mode="r", token=self.token)
    kb = artifact.GetArtifactKnowledgeBase(client)

    for response in responses:
      runkey = response.registry_data.string

      path_guesses = utils.GuessWindowsFileNameFromString(runkey)
      path_guesses = filter(self._IsExecutableExtension, path_guesses)
      if not path_guesses:
        self.Log("Couldn't guess path for %s", runkey)

      for path in path_guesses:
        full_path = artifact_lib.ExpandWindowsEnvironmentVariables(path, kb)
        filenames.append(rdfvalue.PathSpec(
            path=full_path, pathtype=rdfvalue.PathSpec.PathType.TSK))

    if filenames:
      self.CallFlow("MultiGetFile", pathspecs=filenames,
                    next_state="Done")

  @flow.StateHandler()
  def Done(self, responses):
    for response in responses:
      self.SendReply(response)


class GetMRU(flow.GRRFlow):
  """Collect a list of the Most Recently Used files for all users."""

  category = "/Registry/"
  behaviours = flow.GRRFlow.behaviours + "BASIC"

  @flow.StateHandler(next_state="StoreMRUs")
  def Start(self):
    """Call the find flow to get the MRU data for each user."""
    fd = aff4.FACTORY.Open(self.client_id, mode="r", token=self.token)
    for user in fd.Get(fd.Schema.USER):
      mru_path = ("HKEY_USERS/%s/Software/Microsoft/Windows"
                  "/CurrentVersion/Explorer/ComDlg32"
                  "/OpenSavePidlMRU" % user.sid)

      findspec = rdfvalue.FindSpec(max_depth=2, path_regex=".")
      findspec.iterator.number = 1000
      findspec.pathspec.path = mru_path
      findspec.pathspec.pathtype = rdfvalue.PathSpec.PathType.REGISTRY

      self.CallFlow("FindFiles", findspec=findspec, output=None,
                    next_state="StoreMRUs",
                    request_data=dict(username=user.username))

  @flow.StateHandler()
  def StoreMRUs(self, responses):
    """Store the MRU data for each user in a special structure."""
    for response in responses:
      urn = aff4.AFF4Object.VFSGRRClient.PathspecToURN(
          response.pathspec, self.client_id)

      if stat.S_ISDIR(response.st_mode):
        obj_type = "VFSDirectory"
      else:
        obj_type = "VFSFile"

      fd = aff4.FACTORY.Create(urn, obj_type, mode="w", token=self.token)
      fd.Set(fd.Schema.STAT(response))
      fd.Close(sync=False)

      username = responses.request_data["username"]

      m = re.search("/([^/]+)/\\d+$", unicode(urn))
      if m:
        extension = m.group(1)
        fd = aff4.FACTORY.Create(
            rdfvalue.RDFURN(self.client_id)
            .Add("analysis/MRU/Explorer")
            .Add(extension)
            .Add(username),
            "MRUCollection", token=self.token,
            mode="rw")

        # TODO(user): Implement the actual parsing of the MRU.
        mrus = fd.Get(fd.Schema.LAST_USED_FOLDER)
        mrus.Append(filename="Foo")

        fd.Set(mrus)
        fd.Close()
