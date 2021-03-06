#!/usr/bin/env python
# -*- mode: python; encoding: utf-8 -*-

"""Tests for artifacts."""



import os
import subprocess
import time

# pylint: disable=unused-import,g-bad-import-order
from grr.lib import server_plugins
# Pull in some extra artifacts used for testing.
from grr.lib import artifact_lib_test
# pylint: enable=unused-import,g-bad-import-order

from grr.client import client_utils_linux
from grr.client import client_utils_osx
from grr.client import vfs
from grr.client.client_actions import standard
from grr.lib import aff4
from grr.lib import artifact
from grr.lib import artifact_lib
from grr.lib import config_lib
from grr.lib import flags
from grr.lib import flow
from grr.lib import parsers
from grr.lib import rdfvalue
from grr.lib import test_lib
from grr.lib import utils


class GRRArtifactTest(test_lib.GRRBaseTest):

  def testRDFMaps(self):
    """Validate the RDFMaps."""
    for rdf_name, dat in artifact.GRRArtifactMappings.rdf_map.items():
      # "info/software", "InstalledSoftwarePackages", "INSTALLED_PACKAGES",
      # "Append"
      _, aff4_type, aff4_attribute, operator = dat

      if operator not in ["Set", "Append"]:
        raise artifact_lib.ArtifactDefinitionError(
            "Bad RDFMapping, unknown operator %s in %s" %
            (operator, rdf_name))

      if aff4_type not in aff4.AFF4Object.classes:
        raise artifact_lib.ArtifactDefinitionError(
            "Bad RDFMapping, invalid AFF4 Object %s in %s" %
            (aff4_type, rdf_name))

      attr = getattr(aff4.AFF4Object.classes[aff4_type].SchemaCls,
                     aff4_attribute)()
      if not isinstance(attr, rdfvalue.RDFValue):
        raise artifact_lib.ArtifactDefinitionError(
            "Bad RDFMapping, bad attribute %s for %s" %
            (aff4_attribute, rdf_name))


WMI_SAMPLE = [
    rdfvalue.Dict({u"Version": u"65.61.49216", u"InstallDate2": u"",
                   u"Name": u"Google Chrome", u"Vendor": u"Google, Inc.",
                   u"Description": u"Google Chrome", u"IdentifyingNumber":
                   u"{35790B21-ACFE-33F5-B320-9DA320D96682}",
                   u"InstallDate": u"20130710"}),
    rdfvalue.Dict({u"Version": u"7.0.1", u"InstallDate2": u"",
                   u"Name": u"Parity Agent", u"Vendor": u"Bit9, Inc.",
                   u"Description": u"Parity Agent", u"IdentifyingNumber":
                   u"{ADC7EB41-4CC2-4FBA-8FBE-9338A9FB7666}",
                   u"InstallDate": u"20130710"}),
    rdfvalue.Dict({u"Version": u"8.0.61000", u"InstallDate2": u"",
                   u"Name": u"Microsoft Visual C++ 2005 Redistributable (x64)",
                   u"Vendor": u"Microsoft Corporation", u"Description":
                   u"Microsoft Visual C++ 2005 Redistributable (x64)",
                   u"IdentifyingNumber":
                   u"{ad8a2fa1-06e7-4b0d-927d-6e54b3d3102}",
                   u"InstallDate": u"20130710"})]


class TestCmdProcessor(parsers.CommandParser):

  output_types = ["SoftwarePackage"]
  supported_artifacts = ["TestCmdArtifact"]

  def Parse(self, cmd, args, stdout, stderr, return_val, time_taken,
            knowledge_base):
    _ = cmd, args, stdout, stderr, return_val, time_taken, knowledge_base
    installed = rdfvalue.SoftwarePackage.InstallState.INSTALLED
    soft = rdfvalue.SoftwarePackage(name="Package1", description="Desc1",
                                    version="1", architecture="amd64",
                                    install_state=installed)
    yield soft
    soft = rdfvalue.SoftwarePackage(name="Package2", description="Desc2",
                                    version="1", architecture="i386",
                                    install_state=installed)
    yield soft

    # Also yield something random so we can test return type filtering.
    yield rdfvalue.StatEntry()

    # Also yield an anomaly to test that.
    yield rdfvalue.Anomaly(type="PARSER_ANOMALY",
                           symptom="could not parse gremlins.")


class ArtifactTestHelper(test_lib.FlowTestsBaseclass):
  """Helper class for tests using artifacts."""

  @classmethod
  def LoadTestArtifacts(cls):
    test_artifacts_file = os.path.join(
        config_lib.CONFIG["Test.data_dir"], "test_artifacts.json")
    artifact_lib.LoadArtifactsFromFiles([test_artifacts_file])

  class MockClient(test_lib.MemoryClientMock):
    def WmiQuery(self, _):
      return WMI_SAMPLE

    def VolatilityAction(self, _):
      ps_list_file = os.path.join(config_lib.CONFIG["Test.data_dir"],
                                  "vol_pslist_result.dat")
      serialized_result = open(ps_list_file).read(100000)
      return [rdfvalue.VolatilityResult(serialized_result)]

  def SetWindowsClient(self):
    fd = aff4.FACTORY.Open(self.client_id, token=self.token, mode="rw")
    fd.Set(fd.Schema.SYSTEM("Windows"))
    fd.Set(fd.Schema.OS_VERSION("6.2"))
    fd.Set(fd.Schema.ARCH("AMD64"))
    fd.Flush()

  def SetLinuxClient(self):
    fd = aff4.FACTORY.Open(self.client_id, token=self.token, mode="rw")
    fd.Set(fd.Schema.SYSTEM("Linux"))
    fd.Flush()

  def MockClientMountPointsWithImage(self, image_path, fs_type="ext2"):
    """Mock the client to run off a test image."""
    def MockGetMountpoints():
      return {"/": (image_path, fs_type)}
    self.orig_linux_mp = client_utils_linux.GetMountpoints
    self.orig_osx_mp = client_utils_osx.GetMountpoints
    client_utils_linux.GetMountpoints = MockGetMountpoints
    client_utils_osx.GetMountpoints = MockGetMountpoints
    # We wiped the data_store so we have to retransmit all blobs.
    standard.HASH_CACHE = utils.FastStore(100)

  def UnMockClientMountPoints(self):
    """Restore mocked mount points."""
    client_utils_linux.GetMountpoints = self.orig_linux_mp
    client_utils_osx.GetMountpoints = self.orig_osx_mp

  def RunCollectorAndGetCollection(self, artifact_list, client_mock=None,
                                   **kw):
    """Helper to handle running the collector flow."""
    if client_mock is None:
      client_mock = self.MockClient()
    output_name = "/analysis/output/%s" % int(time.time())

    for _ in test_lib.TestFlowHelper(
        "ArtifactCollectorFlow", client_mock=client_mock, output=output_name,
        client_id=self.client_id, artifact_list=artifact_list,
        token=self.token, **kw):
      pass

    output_urn = self.client_id.Add(output_name)
    return aff4.FACTORY.Open(output_urn, aff4_type="RDFValueCollection",
                             token=self.token)


class ArtifactFlowTest(ArtifactTestHelper):

  def setUp(self):
    """Make sure things are initialized."""
    super(ArtifactFlowTest, self).setUp()
    fd = aff4.FACTORY.Open(self.client_id, token=self.token, mode="rw")
    fd.Set(fd.Schema.SYSTEM("Linux"))
    kb = fd.Schema.KNOWLEDGE_BASE()
    artifact.SetCoreGRRKnowledgeBaseValues(kb, fd)
    kb.MergeOrAddUser(rdfvalue.KnowledgeBaseUser(username="gogol"))
    kb.MergeOrAddUser(rdfvalue.KnowledgeBaseUser(username="gevulot"))
    kb.MergeOrAddUser(rdfvalue.KnowledgeBaseUser(username="exomemory"))
    fd.Set(kb)
    fd.Flush()
    self.LoadTestArtifacts()

  def testCmdArtifact(self):
    """Check we can run command based artifacts and get anomalies."""

    class Popen(object):
      """A mock object for subprocess.Popen."""

      def __init__(self, run, stdout, stderr, stdin):
        Popen.running_args = run
        Popen.stdout = stdout
        Popen.stderr = stderr
        Popen.stdin = stdin
        Popen.returncode = 0

      def communicate(self):  # pylint: disable=g-bad-name
        return "stdout here", "stderr here"

    client_mock = self.MockClient("ExecuteCommand")
    with test_lib.Stubber(subprocess, "Popen", Popen):
      for _ in test_lib.TestFlowHelper(
          "ArtifactCollectorFlow", client_mock, client_id=self.client_id,
          store_results_in_aff4=True, use_tsk=False,
          artifact_list=["TestCmdArtifact"], token=self.token):
        pass
    urn = self.client_id.Add("info/software")
    fd = aff4.FACTORY.Open(urn, token=self.token)
    packages = fd.Get(fd.Schema.INSTALLED_PACKAGES)
    self.assertEquals(len(packages), 2)
    self.assertEquals(packages[0].__class__.__name__, "SoftwarePackage")

    with aff4.FACTORY.Open(self.client_id.Add("anomalies"),
                           token=self.token) as anomaly_coll:
      self.assertEquals(len(anomaly_coll), 1)
      self.assertTrue("gremlin" in anomaly_coll[0].symptom)

  def testWMIQueryArtifact(self):
    """Check we can run WMI based artifacts."""
    self.SetWindowsClient()
    self.RunCollectorAndGetCollection(["WindowsWMIInstalledSoftware"],
                                      store_results_in_aff4=True)
    urn = self.client_id.Add("info/software")
    fd = aff4.FACTORY.Open(urn, token=self.token)
    packages = fd.Get(fd.Schema.INSTALLED_PACKAGES)
    self.assertEquals(len(packages), 3)
    self.assertEquals(packages[0].description, "Google Chrome")

  def testVolatilityArtifact(self):
    """Check we can run volatility based artifacts."""
    self.SetWindowsClient()
    self.CreateSignedDriver()
    fd = self.RunCollectorAndGetCollection(["VolatilityPsList"])
    self.assertEquals(len(fd), 50)
    self.assertEquals(fd[0].exe, "System")
    self.assertEquals(fd[0].pid, 4)

  def testFileArtifact(self):
    """Check we can run command based artifacts."""
    # Update the artifact path to point to the test directory.
    art_reg = artifact_lib.ArtifactRegistry.artifacts
    art_reg["TestFileArtifact"].collectors[0].args["path"] = (
        os.path.join(self.base_path, "auth.log"))

    client_mock = test_lib.ActionMock("TransferBuffer", "StatFile", "Find",
                                      "HashBuffer", "ListDirectory",
                                      "HashFile")
    self.RunCollectorAndGetCollection(["TestFileArtifact"],
                                      client_mock=client_mock)
    urn = self.client_id.Add("fs/os/").Add(self.base_path).Add("auth.log")
    aff4.FACTORY.Open(urn, aff4_type="VFSBlobImage", token=self.token)

  def testFilesArtifact(self):
    """Check we can run command based artifacts."""
    # Update the artifact path to point to the test directory.
    art_reg = artifact_lib.ArtifactRegistry.artifacts
    art_reg["TestFilesArtifact"].collectors[0].args["path_list"] = (
        [os.path.join(self.base_path, "auth.log")])
    client_mock = test_lib.ActionMock("TransferBuffer", "StatFile", "Find",
                                      "HashBuffer", "ListDirectory",
                                      "HashFile")
    self.RunCollectorAndGetCollection(["TestFilesArtifact"],
                                      client_mock=client_mock)
    urn = self.client_id.Add("fs/os/").Add(self.base_path).Add("auth.log")
    aff4.FACTORY.Open(urn, aff4_type="VFSBlobImage", token=self.token)

  def testLinuxPasswdHomedirsArtifact(self):
    """Check we can run command based artifacts."""
    # Update the artifact path to point to the test directory.
    art_reg = artifact_lib.ArtifactRegistry.artifacts
    art_reg["LinuxPasswdHomedirs"].collectors[0].args["path_list"] = [
        os.path.join(self.base_path, "passwd")]

    client_mock = test_lib.ActionMock("TransferBuffer", "StatFile", "Find",
                                      "HashBuffer", "ListDirectory",
                                      "HashFile", "Grep")
    fd = self.RunCollectorAndGetCollection(["LinuxPasswdHomedirs"],
                                           client_mock=client_mock)

    self.assertEqual(len(fd), 3)
    self.assertItemsEqual([x.username for x in fd], [u"exomemory", u"gevulot",
                                                     u"gogol"])
    for user in fd:
      if user.username == u"exomemory":
        self.assertEqual(user.full_name, u"Never Forget (admin)")
        self.assertEqual(user.gid, 47)
        self.assertEqual(user.homedir, u"/var/lib/exomemory")
        self.assertEqual(user.shell, u"/bin/sh")
        self.assertEqual(user.uid, 46)

  def testArtifactOutput(self):
    """Check we can run command based artifacts."""
    self.SetLinuxClient()

    # Update the artifact path to point to the test directory.
    art_reg = artifact_lib.ArtifactRegistry.artifacts
    art_reg["TestFileArtifact"].collectors[0].args["path"] = (
        os.path.join(self.base_path, "auth.log"))

    client_mock = test_lib.ActionMock("TransferBuffer", "StatFile", "HashFile",
                                      "HashBuffer", "ListDirectory", "Find")
    # Will raise if something goes wrong.
    self.RunCollectorAndGetCollection(["TestFileArtifact"],
                                      client_mock=client_mock)

    # Will raise if something goes wrong.
    self.RunCollectorAndGetCollection(["TestFileArtifact"],
                                      client_mock=client_mock,
                                      split_output_by_artifact=True)

    # Test the no_results_errors option.
    with self.assertRaises(RuntimeError) as context:
      self.RunCollectorAndGetCollection(
          ["NullArtifact"], client_mock=client_mock,
          split_output_by_artifact=True, no_results_errors=True)
    if "collector returned 0 responses" not in str(context.exception):
      raise RuntimeError("0 responses should have been returned")


class GrrKbTest(test_lib.FlowTestsBaseclass):
  def SetupMocks(self):
    test_lib.ClientFixture(self.client_id, token=self.token)

    client = aff4.FACTORY.Open(self.client_id, token=self.token, mode="rw")
    client.Set(client.Schema.SYSTEM("Windows"))
    client.Set(client.Schema.OS_VERSION("6.2"))
    client.Flush()

    vfs.VFS_HANDLERS[
        rdfvalue.PathSpec.PathType.REGISTRY] = test_lib.ClientRegistryVFSFixture
    vfs.VFS_HANDLERS[
        rdfvalue.PathSpec.PathType.OS] = test_lib.ClientFullVFSFixture

  def testKnowledgeBaseRetrievalWindows(self):
    """Check we can retrieve a knowledge base from a client."""
    self.SetupMocks()

    client_mock = test_lib.ActionMock("TransferBuffer", "StatFile", "Find",
                                      "HashBuffer", "ListDirectory", "HashFile")

    for _ in test_lib.TestFlowHelper(
        "KnowledgeBaseInitializationFlow", client_mock,
        client_id=self.client_id, token=self.token):
      pass

    # The client should now be populated with the data we care about.
    client = aff4.FACTORY.Open(self.client_id, token=self.token)
    kb = artifact.GetArtifactKnowledgeBase(client)
    self.assertEqual(kb.environ_systemroot, "C:\\Windows")
    self.assertEqual(kb.time_zone, "US/Alaska")
    self.assertEqual(kb.code_page, "cp_1252")

    self.assertEqual(kb.environ_windir, "C:\\Windows")
    self.assertEqual(kb.environ_allusersprofile, "C:\\Users\\All Users")
    self.assertEqual(kb.environ_allusersappdata, "C:\\ProgramData")
    self.assertEqual(kb.environ_temp, "C:\\Windows\\TEMP")
    self.assertEqual(kb.environ_systemdrive, "C:")

    self.assertItemsEqual([x.username for x in kb.users],
                          ["jim", "kovacs"])
    user = kb.GetUser(username="jim")
    self.assertEqual(user.username, "jim")
    self.assertEqual(user.sid, "S-1-5-21-702227068-2140022151-3110739409-1000")

  def testKnowledgeBaseRetrievalFailures(self):
    """Test kb retrieval failure modes."""
    client = aff4.FACTORY.Open(self.client_id, token=self.token, mode="rw")
    self.assertRaises(artifact_lib.KnowledgeBaseUninitializedError,
                      artifact.GetArtifactKnowledgeBase, client)
    kb = rdfvalue.KnowledgeBase()
    kb.hostname = "test"
    client.Set(client.Schema.KNOWLEDGE_BASE(kb))
    client.Flush(sync=True)
    self.assertRaises(artifact_lib.KnowledgeBaseAttributesMissingError,
                      artifact.GetArtifactKnowledgeBase, client)

  def testKnowledgeBaseRetrievalDarwin(self):
    """Check we can retrieve a Darwin kb."""
    test_lib.ClientFixture(self.client_id, token=self.token)
    vfs.VFS_HANDLERS[
        rdfvalue.PathSpec.PathType.OS] = test_lib.ClientVFSHandlerFixture
    client = aff4.FACTORY.Open(self.client_id, token=self.token, mode="rw")
    client.Set(client.Schema.SYSTEM("Darwin"))
    client.Set(client.Schema.OS_VERSION("10.9"))
    client.Close()

    client_mock = test_lib.ActionMock("TransferBuffer", "StatFile", "Find",
                                      "HashBuffer", "ListDirectory", "HashFile")

    for _ in test_lib.TestFlowHelper(
        "KnowledgeBaseInitializationFlow", client_mock,
        client_id=self.client_id, token=self.token):
      pass
    client = aff4.FACTORY.Open(self.client_id, token=self.token, mode="rw")
    kb = artifact.GetArtifactKnowledgeBase(client)
    self.assertEqual(kb.os_major_version, 10)
    self.assertEqual(kb.os_minor_version, 9)
    # scalzi from /Users dir listing.
    # Bert and Ernie not present (Users fixture overriden by kb).
    self.assertItemsEqual([x.username for x in kb.users], ["scalzi"])
    user = kb.GetUser(username="scalzi")
    self.assertEqual(user.homedir, "/Users/scalzi")

  def testKnowledgeBaseRetrievalLinux(self):
    """Check we can retrieve a Linux kb."""
    test_lib.ClientFixture(self.client_id, token=self.token)
    config_lib.CONFIG.Set("Artifacts.knowledge_base", ["LinuxWtmp",
                                                       "NetgroupConfiguration"])
    config_lib.CONFIG.Set("Artifacts.netgroup_filter_regexes", ["^login$"])
    config_lib.CONFIG.Set("Artifacts.netgroup_user_blacklist", ["isaac"])

    vfs.VFS_HANDLERS[
        rdfvalue.PathSpec.PathType.OS] = test_lib.ClientTestDataVFSFixture
    client = aff4.FACTORY.Open(self.client_id, token=self.token, mode="rw")
    client.Set(client.Schema.SYSTEM("Linux"))
    client.Set(client.Schema.OS_VERSION("12.04"))
    client.Close()

    client_mock = test_lib.ActionMock("TransferBuffer", "StatFile", "Find",
                                      "HashBuffer", "ListDirectory", "HashFile")

    for _ in test_lib.TestFlowHelper(
        "KnowledgeBaseInitializationFlow", client_mock,
        client_id=self.client_id, token=self.token):
      pass
    client = aff4.FACTORY.Open(self.client_id, token=self.token, mode="rw")
    kb = artifact.GetArtifactKnowledgeBase(client)
    self.assertEqual(kb.os_major_version, 12)
    self.assertEqual(kb.os_minor_version, 4)
    # user 1,2,3 from wtmp. yagharek from netgroup.
    # Bert and Ernie not present (Users fixture overriden by kb).
    self.assertItemsEqual([x.username for x in kb.users], ["user1", "user2",
                                                           "user3", "yagharek"])
    user = kb.GetUser(username="user1")
    self.assertEqual(user.last_logon.AsSecondsFromEpoch(), 1296552099)

  def testKnowledgeBaseRetrievalLinuxNoUsers(self):
    """Cause a users.username dependency failure."""
    test_lib.ClientFixture(self.client_id, token=self.token)
    config_lib.CONFIG.Set("Artifacts.knowledge_base",
                          ["NetgroupConfiguration",
                           "NssCacheLinuxPasswdHomedirs"])
    config_lib.CONFIG.Set("Artifacts.netgroup_filter_regexes",
                          ["^doesntexist$"])

    vfs.VFS_HANDLERS[
        rdfvalue.PathSpec.PathType.OS] = test_lib.ClientTestDataVFSFixture
    client = aff4.FACTORY.Open(self.client_id, token=self.token, mode="rw")
    client.Set(client.Schema.SYSTEM("Linux"))
    client.Set(client.Schema.OS_VERSION("12.04"))
    client.Close()

    client_mock = test_lib.ActionMock("TransferBuffer", "StatFile", "Find",
                                      "HashBuffer", "ListDirectory", "HashFile")

    for _ in test_lib.TestFlowHelper(
        "KnowledgeBaseInitializationFlow", client_mock,
        require_complete=False,
        client_id=self.client_id, token=self.token):
      pass
    client = aff4.FACTORY.Open(self.client_id, token=self.token, mode="rw")
    kb = artifact.GetArtifactKnowledgeBase(client)
    self.assertEqual(kb.os_major_version, 12)
    self.assertEqual(kb.os_minor_version, 4)
    self.assertItemsEqual([x.username for x in kb.users], [])

  def testKnowledgeBaseNoOS(self):
    """Check unset OS dies."""
    vfs.VFS_HANDLERS[
        rdfvalue.PathSpec.PathType.OS] = test_lib.ClientVFSHandlerFixture
    client_mock = test_lib.ActionMock("TransferBuffer", "StatFile", "Find",
                                      "HashBuffer", "ListDirectory", "HashFile")

    self.assertRaises(flow.FlowError, list, test_lib.TestFlowHelper(
        "KnowledgeBaseInitializationFlow", client_mock,
        client_id=self.client_id, token=self.token))

  def testGlobRegistry(self):
    """Test that glob works on registry."""
    self.SetupMocks()

    client_mock = test_lib.ActionMock("TransferBuffer", "StatFile", "Find",
                                      "HashBuffer", "ListDirectory")

    paths = ["HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Windows NT"
             "\\CurrentVersion\\ProfileList\\ProfilesDirectory",
             "HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Windows NT"
             "\\CurrentVersion\\ProfileList\\AllUsersProfile"]

    for _ in test_lib.TestFlowHelper(
        "Glob", client_mock, paths=paths,
        pathtype=rdfvalue.PathSpec.PathType.REGISTRY,
        client_id=self.client_id, token=self.token):
      pass

    path = paths[0].replace("\\", "/")

    fd = aff4.FACTORY.Open(self.client_id.Add("registry").Add(path),
                           token=self.token)
    self.assertEqual(fd.__class__.__name__, "VFSFile")
    self.assertEqual(fd.Get(fd.Schema.STAT).registry_data.GetValue(),
                     "%SystemDrive%\\Users")

  def testGetDependencies(self):
    """Test that dependencies are calculated correctly."""
    self.SetupMocks()
    with test_lib.Stubber(artifact_lib.ArtifactRegistry, "artifacts", {}):
      test_artifacts_file = os.path.join(
          config_lib.CONFIG["Test.data_dir"], "test_artifacts.json")
      artifact_lib.LoadArtifactsFromFiles([test_artifacts_file])

      config_lib.CONFIG.Set("Artifacts.knowledge_base", ["DepsParent",
                                                         "DepsDesktop",
                                                         "DepsHomedir",
                                                         "DepsWindir",
                                                         "DepsWindirRegex",
                                                         "DepsControlSet",
                                                         "FakeArtifact"])
      config_lib.CONFIG.Set("Artifacts.knowledge_base_additions",
                            ["DepsHomedir2"])
      config_lib.CONFIG.Set("Artifacts.knowledge_base_skip", ["DepsWindir"])
      config_lib.CONFIG.Set("Artifacts.knowledge_base_heavyweight",
                            ["FakeArtifact"])
      args = rdfvalue.KnowledgeBaseInitializationArgs(lightweight=True)
      kb_init = artifact.KnowledgeBaseInitializationFlow(None, token=self.token)
      kb_init.args = args
      kb_init.state.Register("all_deps", set())
      kb_init.state.Register("awaiting_deps_artifacts", [])
      kb_init.state.Register("knowledge_base",
                             rdfvalue.KnowledgeBase(os="Windows"))
      no_deps, all_deps, waiting = kb_init._GetDependencies()
      self.assertItemsEqual(no_deps, ["DepsControlSet", "DepsHomedir2"])
      self.assertItemsEqual(all_deps, ["users.homedir", "users.desktop",
                                       "users.username", "environ_windir",
                                       "current_control_set"])
      self.assertItemsEqual(waiting, ["DepsParent", "DepsDesktop",
                                      "DepsHomedir", "DepsWindirRegex"])


def main(argv):
  test_lib.main(argv)

if __name__ == "__main__":
  flags.StartMain(main)
