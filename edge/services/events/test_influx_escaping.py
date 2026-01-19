#!/usr/bin/env python3
"""
Unit tests for Influx Line Protocol escaping functions.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import escape_tag_value, escape_field_string, escape_measurement


def test_escape_tag_value():
    """Test tag value escaping (commas, equals, spaces)."""
    # Simple values
    assert escape_tag_value("simple") == "simple"
    assert escape_tag_value("test_123") == "test_123"
    
    # Special characters
    assert escape_tag_value("tag,value") == "tag\\,value"
    assert escape_tag_value("key=val") == "key\\=val"
    assert escape_tag_value("has space") == "has\\ space"
    assert escape_tag_value("all,special=chars here") == "all\\,special\\=chars\\ here"
    
    # Backslashes
    assert escape_tag_value("back\\slash") == "back\\\\slash"
    assert escape_tag_value("\\leading") == "\\\\leading"
    
    # Combined
    assert escape_tag_value("system,id=123 test") == "system\\,id\\=123\\ test"
    
    # Empty
    assert escape_tag_value("") == ""
    assert escape_tag_value(None) == ""
    
    print("✓ test_escape_tag_value passed")


def test_escape_field_string():
    """Test field string value escaping (quotes and backslashes)."""
    # Simple strings
    assert escape_field_string("hello") == '"hello"'
    assert escape_field_string("test 123") == '"test 123"'
    
    # Quotes
    assert escape_field_string('say "hello"') == '"say \\"hello\\""'
    assert escape_field_string("it's ok") == '"it\'s ok"'
    
    # Backslashes
    assert escape_field_string("path\\to\\file") == '"path\\\\to\\\\file"'
    assert escape_field_string("\\start") == '"\\\\start"'
    
    # Combined
    assert escape_field_string('msg="value"\\path') == '"msg=\\"value\\"\\\\path"'
    
    # Empty
    assert escape_field_string("") == '""'
    assert escape_field_string(None) == '""'
    
    # Special case: newlines and tabs (should be preserved in quotes)
    assert escape_field_string("line1\nline2") == '"line1\nline2"'
    assert escape_field_string("tab\there") == '"tab\there"'
    
    print("✓ test_escape_field_string passed")


def test_escape_measurement():
    """Test measurement name escaping (commas and spaces)."""
    # Simple names
    assert escape_measurement("metric_name") == "metric_name"
    assert escape_measurement("ovr_event_active") == "ovr_event_active"
    
    # Special characters
    assert escape_measurement("metric,name") == "metric\\,name"
    assert escape_measurement("has space") == "has\\ space"
    assert escape_measurement("both, here") == "both\\,\\ here"
    
    # Backslashes
    assert escape_measurement("back\\slash") == "back\\\\slash"
    
    # Empty
    assert escape_measurement("") == "metric"
    assert escape_measurement(None) == "metric"
    
    print("✓ test_escape_measurement passed")


def test_complete_line_protocol():
    """Test complete Influx line protocol generation."""
    from app import escape_tag_value, escape_field_string, escape_measurement
    
    # Example 1: Event start
    system_id = "rig,01"  # has comma
    event_id = "test=123"  # has equals
    ts = 1641024000000000000  # nanoseconds
    
    line = (
        f"{escape_measurement('ovr_event_active')},"
        f"system_id={escape_tag_value(system_id)},"
        f"event_id={escape_tag_value(event_id)} "
        f"v=1i {ts}"
    )
    
    expected = "ovr_event_active,system_id=rig\\,01,event_id=test\\=123 v=1i 1641024000000000000"
    assert line == expected, f"Expected: {expected}\nGot: {line}"
    
    # Example 2: Location with spaces
    location = "Field Site A"
    line = (
        f"{escape_measurement('ovr_location')},"
        f"system_id={escape_tag_value(system_id)},"
        f"location={escape_tag_value(location)} "
        f"v=1i {ts}"
    )
    
    expected = "ovr_location,system_id=rig\\,01,location=Field\\ Site\\ A v=1i 1641024000000000000"
    assert line == expected, f"Expected: {expected}\nGot: {line}"
    
    # Example 3: Note with special characters
    note_msg = 'Operator said: "Voltage dropped", check logs\\data'
    line = (
        f"{escape_measurement('ovr_event_note')},"
        f"system_id={escape_tag_value(system_id)},"
        f"event_id={escape_tag_value(event_id)} "
        f"msg={escape_field_string(note_msg)} {ts}"
    )
    
    expected = 'ovr_event_note,system_id=rig\\,01,event_id=test\\=123 msg="Operator said: \\"Voltage dropped\\", check logs\\\\data" 1641024000000000000'
    assert line == expected, f"Expected: {expected}\nGot: {line}"
    
    print("✓ test_complete_line_protocol passed")


if __name__ == "__main__":
    print("Running Influx Line Protocol escaping tests...\n")
    
    test_escape_tag_value()
    test_escape_field_string()
    test_escape_measurement()
    test_complete_line_protocol()
    
    print("\n✅ All tests passed!")
