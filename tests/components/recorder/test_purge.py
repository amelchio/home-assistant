"""Test data purging."""
import json
from datetime import datetime, timedelta
import unittest

from homeassistant.components import recorder
from homeassistant.components.recorder.const import DATA_INSTANCE
from homeassistant.components.recorder.purge import purge_old_data
from homeassistant.components.recorder.models import States, Events
from homeassistant.components.recorder.util import session_scope
from tests.common import get_test_home_assistant, init_recorder_component


class TestRecorderPurge(unittest.TestCase):
    """Base class for common recorder tests."""

    def setUp(self):  # pylint: disable=invalid-name
        """Setup things to be run when tests are started."""
        config = {'purge_keep_days': 4, 'purge_interval': 2}
        self.hass = get_test_home_assistant()
        init_recorder_component(self.hass, config)
        self.hass.start()

    def tearDown(self):  # pylint: disable=invalid-name
        """Stop everything that was started."""
        self.hass.stop()

    def _add_test_states(self):
        """Add multiple states to the db for testing."""
        now = datetime.now()
        five_days_ago = now - timedelta(days=5)
        attributes = {'test_attr': 5, 'test_attr_10': 'nice'}

        with recorder.session_scope(hass=self.hass) as session:
            for event_id in range(5):
                if event_id < 3:
                    timestamp = five_days_ago
                    state = 'purgeme'
                else:
                    timestamp = now
                    state = 'dontpurgeme'

                session.add(States(
                    entity_id='test.recorder2',
                    domain='sensor',
                    state=state,
                    attributes=json.dumps(attributes),
                    last_changed=timestamp,
                    last_updated=timestamp,
                    created=timestamp,
                    event_id=event_id + 1000
                ))

            # if self._add_test_events was called, we added a special event
            # that should be protected from deletion, too
            protected_event_id = getattr(self, "_protected_event_id", 2000)

            # add a state that is old but the only state of its entity and
            # should be protected
            session.add(States(
                entity_id='test.rarely_updated_entity',
                domain='sensor',
                state='iamprotected',
                attributes=json.dumps(attributes),
                last_changed=five_days_ago,
                last_updated=five_days_ago,
                created=five_days_ago,
                event_id=protected_event_id
            ))

    def _add_test_events(self):
        """Add a few events for testing."""
        now = datetime.now()
        five_days_ago = now - timedelta(days=5)
        event_data = {'test_attr': 5, 'test_attr_10': 'nice'}

        with recorder.session_scope(hass=self.hass) as session:
            for event_id in range(5):
                if event_id < 2:
                    timestamp = five_days_ago
                    event_type = 'EVENT_TEST_PURGE'
                else:
                    timestamp = now
                    event_type = 'EVENT_TEST'

                session.add(Events(
                    event_type=event_type,
                    event_data=json.dumps(event_data),
                    origin='LOCAL',
                    created=timestamp,
                    time_fired=timestamp,
                ))

            # Add an event for the protected state
            protected_event = Events(
                event_type='EVENT_TEST_FOR_PROTECTED',
                event_data=json.dumps(event_data),
                origin='LOCAL',
                created=five_days_ago,
                time_fired=five_days_ago,
            )
            session.add(protected_event)
            session.flush()

            self._protected_event_id = protected_event.event_id

    def test_purge_old_states(self):
        """Test deleting old states."""
        self._add_test_states()
        # make sure we start with 6 states
        with session_scope(hass=self.hass) as session:
            states = session.query(States)
            self.assertEqual(states.count(), 6)

            # run purge_old_data()
            purge_old_data(self.hass.data[DATA_INSTANCE], 4)

            # we should only have 3 states left after purging
            self.assertEqual(states.count(), 3)

    def test_purge_old_events(self):
        """Test deleting old events."""
        self._add_test_events()

        with session_scope(hass=self.hass) as session:
            events = session.query(Events).filter(
                Events.event_type.like("EVENT_TEST%"))
            self.assertEqual(events.count(), 6)

            # run purge_old_data()
            purge_old_data(self.hass.data[DATA_INSTANCE], 4)

            # now we should only have 3 events left
            self.assertEqual(events.count(), 3)

    def test_purge_method(self):
        """Test purge method."""
        service_data = {'keep_days': 4}
        self._add_test_events()
        self._add_test_states()

        # make sure we start with 6 states
        with session_scope(hass=self.hass) as session:
            states = session.query(States)
            self.assertEqual(states.count(), 6)

            events = session.query(Events).filter(
                Events.event_type.like("EVENT_TEST%"))
            self.assertEqual(events.count(), 6)

            self.hass.data[DATA_INSTANCE].block_till_done()

            # run purge method - no service data, should not work
            self.hass.services.call('recorder', 'purge')
            self.hass.async_block_till_done()

            # Small wait for recorder thread
            self.hass.data[DATA_INSTANCE].block_till_done()

            # we should still have everything from before
            self.assertEqual(states.count(), 6)
            self.assertEqual(events.count(), 6)

            # run purge method - correct service data
            self.hass.services.call('recorder', 'purge',
                                    service_data=service_data)
            self.hass.async_block_till_done()

            # Small wait for recorder thread
            self.hass.data[DATA_INSTANCE].block_till_done()

            # we should only have 3 states left after purging
            self.assertEqual(states.count(), 3)

            # the protected state is among them
            self.assertTrue('iamprotected' in (
                state.state for state in states))

            # now we should only have 4 events left
            self.assertEqual(events.count(), 4)

            # and the protected event is among them
            self.assertTrue('EVENT_TEST_FOR_PROTECTED' in (
                event.event_type for event in events.all()))
            self.assertFalse('EVENT_TEST_PURGE' in (
                event.event_type for event in events.all()))
