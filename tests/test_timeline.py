"""Timeline semantics: placement, trimming, retiming, and the rules that
stop an edit from quietly becoming something the user did not ask for."""

from __future__ import annotations

import pytest

from hatcut.core.errors import TimelineError
from hatcut.core.timeline import MIN_CLIP_DURATION, Clip, Timeline


def make(source: str = "a.mp4", start: float = 0.0, duration: float = 2.0,
         **kwargs) -> Clip:
    return Clip(source=source, start=start, duration=duration, **kwargs)


class TestPlacement:
    def test_append_lands_at_the_tail(self):
        timeline = Timeline()
        first = timeline.add_clip(make(duration=2.0), track_kind="video")
        second = timeline.add_clip(make(source="b.mp4", start=-1), track_kind="video")
        assert first.start == 0.0
        assert second.start == 2.0

    def test_overlap_is_pushed_to_the_tail_not_stacked(self):
        timeline = Timeline()
        timeline.add_clip(make(start=0.0, duration=3.0), track_kind="video")
        # Asking for a spot in the middle of an existing clip is a request the
        # timeline cannot honour; it serialises instead of piling up.
        second = timeline.add_clip(make(source="b.mp4", start=1.0, duration=2.0),
                                   track_kind="video")
        assert second.start == 3.0

    def test_a_text_clip_lands_on_a_text_track(self):
        timeline = Timeline()
        clip = timeline.add_clip(make(text=None), track_kind="text")
        track, _ = timeline.require_clip(clip.id)
        assert track.kind == "text"
        assert clip.text is not None
        assert timeline.tracks_of("video") == []

    def test_move_onto_an_occupied_span_is_refused(self):
        timeline = Timeline()
        a = timeline.add_clip(make(start=0.0, duration=2.0), track_kind="video")
        timeline.add_clip(make(source="b.mp4", start=2.0, duration=2.0),
                          track_kind="video")
        with pytest.raises(TimelineError, match="overlap"):
            timeline.move_clip(a.id, 1.0)

    def test_move_to_a_free_span_is_allowed(self):
        timeline = Timeline()
        a = timeline.add_clip(make(start=0.0, duration=2.0), track_kind="video")
        timeline.add_clip(make(source="b.mp4", start=4.0, duration=2.0),
                          track_kind="video")
        assert timeline.move_clip(a.id, 2.0).start == 2.0


class TestDuration:
    def test_duration_is_the_longest_track(self):
        timeline = Timeline()
        timeline.add_clip(make(start=0.0, duration=2.0), track_kind="video")
        timeline.add_clip(make(source="b.mp4", start=0.0, duration=5.0),
                          track_kind="audio")
        assert timeline.duration == 5.0

    def test_an_empty_timeline_is_zero_long(self):
        assert Timeline().duration == 0.0
        assert Timeline().is_empty()

    def test_clip_end_accounts_for_start_and_duration(self):
        clip = make(start=1.5, duration=2.25)
        assert clip.end == pytest.approx(3.75)


class TestTrimming:
    def test_trimming_the_tail_shortens_without_moving_the_head(self):
        timeline = Timeline()
        clip = timeline.add_clip(make(duration=4.0), track_kind="video")
        timeline.trim_clip(clip.id, duration=2.0)
        assert clip.start == 0.0
        assert clip.duration == 2.0

    def test_in_point_cannot_go_negative(self):
        timeline = Timeline()
        clip = timeline.add_clip(make(duration=4.0, in_point=1.0), track_kind="video")
        timeline.trim_clip(clip.id, in_point=-5.0)
        assert clip.in_point == 0.0

    def test_source_span_scales_with_speed(self):
        clip = make(duration=2.0, speed=2.0)
        assert clip.source_span == pytest.approx(4.0)


class TestSplit:
    def test_split_produces_two_adjacent_halves(self):
        timeline = Timeline()
        clip = timeline.add_clip(make(duration=4.0), track_kind="video")
        left, right = timeline.split_clip(clip.id, 1.5)

        assert left.duration == pytest.approx(1.5)
        assert right.start == pytest.approx(1.5)
        assert right.duration == pytest.approx(2.5)
        assert left.end == pytest.approx(right.start)

    def test_split_carries_the_in_point_forward(self):
        timeline = Timeline()
        clip = timeline.add_clip(make(duration=4.0, in_point=0.0), track_kind="video")
        _, right = timeline.split_clip(clip.id, 1.0)
        # The right half must resume where the left half stopped, or the cut
        # repeats a second of footage.
        assert right.in_point == pytest.approx(1.0)

    def test_split_outside_the_clip_is_refused(self):
        timeline = Timeline()
        clip = timeline.add_clip(make(duration=2.0), track_kind="video")
        with pytest.raises(TimelineError, match="outside"):
            timeline.split_clip(clip.id, 9.0)

    def test_split_too_close_to_the_edge_is_refused(self):
        timeline = Timeline()
        clip = timeline.add_clip(make(duration=2.0), track_kind="video")
        with pytest.raises(TimelineError):
            timeline.split_clip(clip.id, MIN_CLIP_DURATION / 2)


class TestRippleDelete:
    def test_ripple_pulls_later_clips_back(self):
        timeline = Timeline()
        a = timeline.add_clip(make(start=0.0, duration=2.0), track_kind="video")
        b = timeline.add_clip(make(source="b.mp4", start=2.0, duration=2.0),
                              track_kind="video")
        c = timeline.add_clip(make(source="c.mp4", start=4.0, duration=2.0),
                              track_kind="video")

        timeline.ripple_delete(a.id)

        assert b.start == 0.0
        assert c.start == 2.0
        assert timeline.duration == 4.0

    def test_plain_delete_leaves_the_gap(self):
        timeline = Timeline()
        a = timeline.add_clip(make(start=0.0, duration=2.0), track_kind="video")
        b = timeline.add_clip(make(source="b.mp4", start=2.0, duration=2.0),
                              track_kind="video")
        timeline.remove_clip(a.id)
        assert b.start == 2.0
        assert timeline.duration == 4.0


class TestSpeed:
    def test_faster_means_shorter_on_the_timeline(self):
        timeline = Timeline()
        clip = timeline.add_clip(make(duration=4.0, speed=1.0), track_kind="video")
        timeline.set_speed(clip.id, 2.0)
        # Twice the speed over the same source burns half the timeline.
        assert clip.duration == pytest.approx(2.0)
        assert clip.source_span == pytest.approx(4.0)

    def test_slower_means_longer(self):
        timeline = Timeline()
        clip = timeline.add_clip(make(duration=2.0, speed=1.0), track_kind="video")
        timeline.set_speed(clip.id, 0.5)
        assert clip.duration == pytest.approx(4.0)

    def test_later_clips_are_pushed_to_keep_the_cut_clean(self):
        timeline = Timeline()
        a = timeline.add_clip(make(start=0.0, duration=2.0), track_kind="video")
        b = timeline.add_clip(make(source="b.mp4", start=2.0, duration=2.0),
                              track_kind="video")
        timeline.set_speed(a.id, 0.5)   # a becomes 4s long
        assert b.start == pytest.approx(4.0)


class TestLinkedClips:
    def test_linked_clips_retime_together(self):
        timeline = Timeline()
        picture = timeline.add_clip(
            make(duration=2.0, link_id="L1"), track_kind="video")
        sound = timeline.add_clip(
            make(source="a.mp4", duration=2.0, link_id="L1"), track_kind="audio")
        timeline.set_speed(picture.id, 2.0)
        # A picture that runs fast over audio that does not is a sync bug the
        # user did not ask for.
        assert sound.duration == pytest.approx(1.0)


class TestLinkedTransitions:
    def test_split_keeps_the_incoming_transition_on_the_left_half(self):
        timeline = Timeline()
        clip = timeline.add_clip(make(duration=4.0), track_kind="video")
        clip.transition = {"type": "fade", "duration": 0.5}

        left, right = timeline.split_clip(clip.id, 1.5)

        # The fade belongs to the head of the shot; the tail starts mid-shot
        # and has nothing to fade from.
        assert left.transition == {"type": "fade", "duration": 0.5}
        assert right.transition is None


class TestValidation:
    def test_a_clean_timeline_validates(self):
        timeline = Timeline()
        timeline.add_clip(make(duration=2.0), track_kind="video")
        assert timeline.validate() == []

    def test_odd_dimensions_are_rejected(self):
        timeline = Timeline(width=1281, height=720)
        problems = timeline.validate()
        assert any("even" in p for p in problems)

    def test_a_negative_start_is_read_as_append_not_as_a_negative_time(self):
        timeline = Timeline()
        timeline.add_clip(make(start=0.0, duration=2.0), track_kind="video")
        appended = timeline.add_clip(make(source="b.mp4", start=-1.0,
                                          duration=2.0), track_kind="video")
        assert appended.start == 2.0
        assert timeline.validate() == []

    def test_a_negative_start_is_reported_when_injected_directly(self):
        # add_clip normalises the sentinel, so validation only ever sees this
        # from a malformed project file -- which is precisely when it matters.
        timeline = Timeline()
        track = timeline.ensure_track("video")
        track.clips.append(make(start=-3.0, duration=2.0))
        assert timeline.validate()

    def test_a_degenerate_clip_is_reported(self):
        timeline = Timeline()
        timeline.add_clip(make(duration=MIN_CLIP_DURATION / 10), track_kind="video")
        assert any("shorter" in p for p in timeline.validate())

    def test_an_overlap_is_reported_with_both_names(self):
        timeline = Timeline()
        a = make(start=0.0, duration=3.0)
        b = make(source="b.mp4", start=1.0, duration=3.0)
        track = timeline.ensure_track("video")
        track.clips.extend([a, b])
        problems = timeline.validate()
        assert any("overlap" in p for p in problems)
        assert any(a.id in p or b.id in p for p in problems)
