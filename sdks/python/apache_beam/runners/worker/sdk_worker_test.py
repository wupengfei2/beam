#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""Tests for apache_beam.runners.worker.sdk_worker."""

# pytype: skip-file

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import contextlib
import logging
import unittest
from builtins import range
from collections import namedtuple

import grpc

from apache_beam.coders import VarIntCoder
from apache_beam.portability.api import beam_fn_api_pb2
from apache_beam.portability.api import beam_fn_api_pb2_grpc
from apache_beam.portability.api import beam_runner_api_pb2
from apache_beam.portability.api import metrics_pb2
from apache_beam.runners.worker import sdk_worker
from apache_beam.runners.worker import statecache
from apache_beam.utils import thread_pool_executor

_LOGGER = logging.getLogger(__name__)


class BeamFnControlServicer(beam_fn_api_pb2_grpc.BeamFnControlServicer):
  def __init__(self, requests, raise_errors=True):
    self.requests = requests
    self.instruction_ids = set(r.instruction_id for r in requests)
    self.responses = {}
    self.raise_errors = raise_errors

  def Control(self, response_iterator, context):
    for request in self.requests:
      _LOGGER.info("Sending request %s", request)
      yield request
    for response in response_iterator:
      _LOGGER.info("Got response %s", response)
      if response.instruction_id != -1:
        assert response.instruction_id in self.instruction_ids
        assert response.instruction_id not in self.responses
        self.responses[response.instruction_id] = response
        if self.raise_errors and response.error:
          raise RuntimeError(response.error)
        elif len(self.responses) == len(self.requests):
          _LOGGER.info("All %s instructions finished.", len(self.requests))
          return
    raise RuntimeError(
        "Missing responses: %s" %
        (self.instruction_ids - set(self.responses.keys())))


class SdkWorkerTest(unittest.TestCase):
  def _get_process_bundles(self, prefix, size):
    return [
        beam_fn_api_pb2.ProcessBundleDescriptor(
            id=str(str(prefix) + "-" + str(ix)),
            transforms={
                str(ix): beam_runner_api_pb2.PTransform(unique_name=str(ix))
            }) for ix in range(size)
    ]

  def _check_fn_registration_multi_request(self, *args):
    """Check the function registration calls to the sdk_harness.

    Args:
     tuple of request_count, number of process_bundles per request and workers
     counts to process the request.
    """
    for (request_count, process_bundles_per_request) in args:
      requests = []
      process_bundle_descriptors = []

      for i in range(request_count):
        pbd = self._get_process_bundles(i, process_bundles_per_request)
        process_bundle_descriptors.extend(pbd)
        requests.append(
            beam_fn_api_pb2.InstructionRequest(
                instruction_id=str(i),
                register=beam_fn_api_pb2.RegisterRequest(
                    process_bundle_descriptor=process_bundle_descriptors)))

      test_controller = BeamFnControlServicer(requests)

      server = grpc.server(thread_pool_executor.shared_unbounded_instance())
      beam_fn_api_pb2_grpc.add_BeamFnControlServicer_to_server(
          test_controller, server)
      test_port = server.add_insecure_port("[::]:0")
      server.start()

      harness = sdk_worker.SdkHarness(
          "localhost:%s" % test_port, state_cache_size=100)
      harness.run()

      self.assertEqual(
          harness._bundle_processor_cache.fns,
          {item.id: item
           for item in process_bundle_descriptors})

  def test_fn_registration(self):
    self._check_fn_registration_multi_request((1, 4), (4, 4))


class CachingStateHandlerTest(unittest.TestCase):
  def test_caching(self):

    coder = VarIntCoder()
    coder_impl = coder.get_impl()

    class FakeUnderlyingState(object):
      """Simply returns an incremented counter as the state "value."
      """
      def set_counter(self, n):
        self._counter = n

      def get_raw(self, *args):
        self._counter += 1
        return coder.encode(self._counter), None

      @contextlib.contextmanager
      def process_instruction_id(self, bundle_id):
        yield

    underlying_state = FakeUnderlyingState()
    state_cache = statecache.StateCache(100)
    caching_state_hander = sdk_worker.CachingStateHandler(
        state_cache, underlying_state)

    state1 = beam_fn_api_pb2.StateKey(
        bag_user_state=beam_fn_api_pb2.StateKey.BagUserState(
            user_state_id='state1'))
    state2 = beam_fn_api_pb2.StateKey(
        bag_user_state=beam_fn_api_pb2.StateKey.BagUserState(
            user_state_id='state2'))
    side1 = beam_fn_api_pb2.StateKey(
        multimap_side_input=beam_fn_api_pb2.StateKey.MultimapSideInput(
            transform_id='transform', side_input_id='side1'))
    side2 = beam_fn_api_pb2.StateKey(
        iterable_side_input=beam_fn_api_pb2.StateKey.IterableSideInput(
            transform_id='transform', side_input_id='side2'))

    state_token1 = beam_fn_api_pb2.ProcessBundleRequest.CacheToken(
        token=b'state_token1',
        user_state=beam_fn_api_pb2.ProcessBundleRequest.CacheToken.UserState())
    state_token2 = beam_fn_api_pb2.ProcessBundleRequest.CacheToken(
        token=b'state_token2',
        user_state=beam_fn_api_pb2.ProcessBundleRequest.CacheToken.UserState())
    side1_token1 = beam_fn_api_pb2.ProcessBundleRequest.CacheToken(
        token=b'side1_token1',
        side_input=beam_fn_api_pb2.ProcessBundleRequest.CacheToken.SideInput(
            transform_id='transform', side_input_id='side1'))
    side1_token2 = beam_fn_api_pb2.ProcessBundleRequest.CacheToken(
        token=b'side1_token2',
        side_input=beam_fn_api_pb2.ProcessBundleRequest.CacheToken.SideInput(
            transform_id='transform', side_input_id='side1'))

    def get_as_list(key):
      return list(caching_state_hander.blocking_get(key, coder_impl, True))

    underlying_state.set_counter(100)
    with caching_state_hander.process_instruction_id('bundle1', []):
      self.assertEqual(get_as_list(state1), [101])  # uncached
      self.assertEqual(get_as_list(state2), [102])  # uncached
      self.assertEqual(get_as_list(state1), [101])  # cached on bundle
      self.assertEqual(get_as_list(side1), [103])  # uncached
      self.assertEqual(get_as_list(side2), [104])  # uncached

    underlying_state.set_counter(200)
    with caching_state_hander.process_instruction_id(
        'bundle2', [state_token1, side1_token1]):
      self.assertEqual(get_as_list(state1), [201])  # uncached
      self.assertEqual(get_as_list(state2), [202])  # uncached
      self.assertEqual(get_as_list(state1), [201])  # cached on state token1
      self.assertEqual(get_as_list(side1), [203])  # uncached
      self.assertEqual(get_as_list(side1), [203])  # cached on side1_token1
      self.assertEqual(get_as_list(side2), [204])  # uncached
      self.assertEqual(get_as_list(side2), [204])  # cached on bundle

    underlying_state.set_counter(300)
    with caching_state_hander.process_instruction_id(
        'bundle3', [state_token1, side1_token1]):
      self.assertEqual(get_as_list(state1), [201])  # cached on state token1
      self.assertEqual(get_as_list(state2), [202])  # cached on state token1
      self.assertEqual(get_as_list(state1), [201])  # cached on state token1
      self.assertEqual(get_as_list(side1), [203])  # cached on side1_token1
      self.assertEqual(get_as_list(side1), [203])  # cached on side1_token1
      self.assertEqual(get_as_list(side2), [301])  # uncached
      self.assertEqual(get_as_list(side2), [301])  # cached on bundle

    underlying_state.set_counter(400)
    with caching_state_hander.process_instruction_id(
        'bundle4', [state_token2, side1_token1]):
      self.assertEqual(get_as_list(state1), [401])  # uncached
      self.assertEqual(get_as_list(state2), [402])  # uncached
      self.assertEqual(get_as_list(state1), [401])  # cached on state token2
      self.assertEqual(get_as_list(side1), [203])  # cached on side1_token1
      self.assertEqual(get_as_list(side1), [203])  # cached on side1_token1
      self.assertEqual(get_as_list(side2), [403])  # uncached
      self.assertEqual(get_as_list(side2), [403])  # cached on bundle

    underlying_state.set_counter(500)
    with caching_state_hander.process_instruction_id(
        'bundle5', [state_token2, side1_token2]):
      self.assertEqual(get_as_list(state1), [401])  # cached on state token2
      self.assertEqual(get_as_list(state2), [402])  # cached on state token2
      self.assertEqual(get_as_list(state1), [401])  # cached on state token2
      self.assertEqual(get_as_list(side1), [501])  # uncached
      self.assertEqual(get_as_list(side1), [501])  # cached on side1_token2
      self.assertEqual(get_as_list(side2), [502])  # uncached
      self.assertEqual(get_as_list(side2), [502])  # cached on bundle


class ShortIdCacheTest(unittest.TestCase):
  def testShortIdAssignment(self):
    TestCase = namedtuple('TestCase', ['expectedShortId', 'info'])
    test_cases = [
        TestCase(*args) for args in [
            (
                "1",
                metrics_pb2.MonitoringInfo(
                    urn="beam:metric:user:distribution_int64:v1",
                    type="beam:metrics:distribution_int64:v1")),
            (
                "2",
                metrics_pb2.MonitoringInfo(
                    urn="beam:metric:element_count:v1",
                    type="beam:metrics:sum_int64:v1")),
            (
                "3",
                metrics_pb2.MonitoringInfo(
                    urn="beam:metric:ptransform_progress:completed:v1",
                    type="beam:metrics:progress:v1")),
            (
                "4",
                metrics_pb2.MonitoringInfo(
                    urn="beam:metric:user:distribution_double:v1",
                    type="beam:metrics:distribution_double:v1")),
            (
                "5",
                metrics_pb2.MonitoringInfo(
                    urn="TestingSentinelUrn", type="TestingSentinelType")),
            (
                "6",
                metrics_pb2.MonitoringInfo(
                    urn=
                    "beam:metric:pardo_execution_time:finish_bundle_msecs:v1",
                    type="beam:metrics:sum_int64:v1")),
            # This case and the next one validates that different labels
            # with the same urn are in fact assigned different short ids.
            (
                "7",
                metrics_pb2.MonitoringInfo(
                    urn="beam:metric:user:sum_int64:v1",
                    type="beam:metrics:sum_int64:v1",
                    labels={
                        "PTRANSFORM": "myT",
                        "NAMESPACE": "harness",
                        "NAME": "metricNumber7"
                    })),
            (
                "8",
                metrics_pb2.MonitoringInfo(
                    urn="beam:metric:user:sum_int64:v1",
                    type="beam:metrics:sum_int64:v1",
                    labels={
                        "PTRANSFORM": "myT",
                        "NAMESPACE": "harness",
                        "NAME": "metricNumber8"
                    })),
            (
                "9",
                metrics_pb2.MonitoringInfo(
                    urn="beam:metric:user:top_n_double:v1",
                    type="beam:metrics:top_n_double:v1",
                    labels={
                        "PTRANSFORM": "myT",
                        "NAMESPACE": "harness",
                        "NAME": "metricNumber7"
                    })),
            (
                "a",
                metrics_pb2.MonitoringInfo(
                    urn="beam:metric:element_count:v1",
                    type="beam:metrics:sum_int64:v1",
                    labels={"PCOLLECTION": "myPCol"})),
            # validate payload is ignored for shortId assignment
            (
                "3",
                metrics_pb2.MonitoringInfo(
                    urn="beam:metric:ptransform_progress:completed:v1",
                    type="beam:metrics:progress:v1",
                    payload=b"this is ignored!"))
        ]
    ]

    cache = sdk_worker.ShortIdCache()

    for case in test_cases:
      self.assertEqual(
          case.expectedShortId,
          cache.getShortId(case.info),
          "Got incorrect short id for monitoring info:\n%s" % case.info)

    # Retrieve all of the monitoring infos by short id, and verify that the
    # metadata (everything but the payload) matches the originals
    actual_recovered_infos = cache.getInfos(
        case.expectedShortId for case in test_cases)
    for recoveredInfo, case in zip(actual_recovered_infos, test_cases):
      self.assertEqual(
          monitoringInfoMetadata(case.info),
          monitoringInfoMetadata(recoveredInfo))

    # Retrieve short ids one more time in reverse
    for case in reversed(test_cases):
      self.assertEqual(
          case.expectedShortId,
          cache.getShortId(case.info),
          "Got incorrect short id on second retrieval for monitoring info:\n%s"
          % case.info)


def monitoringInfoMetadata(info):
  return {
      descriptor.name: value
      for descriptor,
      value in info.ListFields() if not descriptor.name == "payload"
  }


if __name__ == "__main__":
  logging.getLogger().setLevel(logging.INFO)
  unittest.main()
