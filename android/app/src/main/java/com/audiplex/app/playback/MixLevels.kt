package com.audiplex.app.playback

import kotlin.math.log10
import kotlin.math.pow

/**
 * #5282 slice 2: every category of sound has its own level, upstream of the phone's volume.
 * 1 is the sound's own level and the default ceiling. Todd 53042: past that is his to choose,
 * up to +12 dB through the limiter, and it can distort. Book at bedtime stops at 1.
 */
enum class MixCategory { MUSIC, BOOK, DJ_VOICE, CHIMES, BED, BOOK_BEDTIME }

/** #5282: pure decisions behind the levels. */
object MixLevels {

    /** A song plays at its dial and track gain. A DJ clip plays at the voice level, whatever the music dial says. */
    fun musicAmplitude(isClip: Boolean, musicDial: Float, trackGain: Double, djVoice: Float, trim: Float): Double =
        (if (isClip) djVoice.toDouble() else musicDial * trackGain) * trim

    /** What a player does with an amplitude: up to 1 is its volume, the rest goes through the limiter. */
    data class Split(val volume: Float, val boostDb: Double)

    fun split(amplitude: Double): Split = when {
        amplitude.isNaN() || amplitude <= 0.0 -> Split(0f, 0.0)
        amplitude <= 1.0 -> Split(amplitude.toFloat(), 0.0)
        else -> Split(1f, (20.0 * log10(amplitude)).coerceAtMost(SLIDER_PAST_FULL_DB))  // a server raise can't stack past it
    }

    private const val SLIDER_FLOOR_DB = -40.0
    private const val SLIDER_PAST_FULL_DB = 12.0
    private const val SNAP_TO_FULL_DB = 0.5  // under the bed's default of 0.9, which must not read as Full

    /** +12 dB as a level. */
    val MAX_LEVEL: Float = 10.0.pow(SLIDER_PAST_FULL_DB / 20.0).toFloat()

    /** Where full sits on the slider, for the mark. */
    val FULL_POSITION: Float = (-SLIDER_FLOOR_DB / (SLIDER_PAST_FULL_DB - SLIDER_FLOOR_DB)).toFloat()

    /** A dB taper from silent through full to +12 dB. Full is sticky, so it stays easy to land on. */
    fun sliderToLevel(position: Float): Float {
        val p = if (position.isNaN()) 0f else position.coerceIn(0f, 1f)
        if (p <= 0f) return 0f
        val db = SLIDER_FLOOR_DB + p * (SLIDER_PAST_FULL_DB - SLIDER_FLOOR_DB)
        return if (kotlin.math.abs(db) < SNAP_TO_FULL_DB) 1f else 10.0.pow(db / 20.0).toFloat()
    }

    fun levelToSlider(level: Float): Float {
        val l = if (level.isNaN()) 0f else level.coerceIn(0f, MAX_LEVEL)
        if (l <= 0f) return 0f
        val db = 20.0 * log10(l.toDouble())
        return ((db - SLIDER_FLOOR_DB) / (SLIDER_PAST_FULL_DB - SLIDER_FLOOR_DB)).coerceIn(0.0, 1.0).toFloat()
    }

    fun levelText(level: Float): String {
        if (level <= 0f) return "Off"
        val db = kotlin.math.round(20.0 * log10(level.toDouble())).toInt()
        return when {
            db == 0 -> "Full"
            db > 0 -> "+$db dB, can distort"
            else -> "$db dB"
        }
    }

    /** Hour and quarter chimes reach the phone as bed_play commands for this path (server dj_triggers.py). */
    private const val CHIME_PATH = "/api/dj/chimes/"

    fun isChimeUrl(url: String?): Boolean = url?.contains(CHIME_PATH) == true

    /** The chime level trims what the server commands; a real bed is left as commanded. */
    fun bedCommandLevel(url: String, commanded: Float, chimeLevel: Float): Float =
        if (isChimeUrl(url)) (commanded * chimeLevel).coerceIn(0f, MAX_LEVEL) else commanded

    /**
     * A DJ clip used to follow the music dial. Start the voice level at that dial, so the update
     * does not make the DJ louder than he left it. Null: already has its own level.
     */
    fun seedDjVoice(storedVoice: Float?, musicDial: Float?): Float? =
        if (storedVoice != null) null else (musicDial ?: 1f).coerceIn(0f, 1f)

    /** How far the server's raises-off setting lowers a typical book (-14 LUFS to -20 LUFS). */
    private const val RAISES_OFF_SHIFT_DB = 6f

    /** What the server's gain response says about book raises. Null: says nothing, change nothing. */
    fun raisesOff(gainMode: String?): Boolean? = when (gainMode) {
        "attenuate_only" -> true
        "boost_needs_limiter" -> false
        else -> null
    }

    /**
     * [appliedDb] is what was added to "Book at bedtime"; null means nothing is applied.
     * [cappedDb] is the part of the shift that did not fit below full.
     */
    data class BedtimeShift(val level: Float, val appliedDb: Float?, val cappedDb: Float)

    /**
     * With raises off the day book drops about 6 dB and the bed does not, so the bedtime level
     * comes up by the same amount to keep the balance. It happens once, because [appliedDb]
     * records it, and undoes itself if the server goes back to raises. Full is the ceiling: what
     * does not fit is reported in [BedtimeShift.cappedDb], so the UI can say so. Null: nothing to do.
     */
    fun bedtimeShift(level: Float, appliedDb: Float?, raisesOff: Boolean): BedtimeShift? {
        val l = if (level.isNaN()) 1f else level.coerceIn(0f, 1f)
        if (raisesOff) {
            if (appliedDb != null) return null
            if (l <= 0f) return BedtimeShift(0f, 0f, 0f)  // silent stays silent, and that is not a cap
            val room = (-20.0 * log10(l.toDouble())).toFloat().coerceAtLeast(0f)
            val applied = minOf(room, RAISES_OFF_SHIFT_DB)
            return BedtimeShift(
                (l * 10.0.pow(applied / 20.0).toFloat()).coerceAtMost(1f),
                applied,
                RAISES_OFF_SHIFT_DB - applied,
            )
        }
        if (appliedDb == null) return null
        return BedtimeShift((l * 10.0.pow(-appliedDb / 20.0).toFloat()).coerceIn(0f, 1f), null, 0f)
    }
}

/**
 * #5282: moves the main player between the song's level and the DJ voice level at item changes.
 * A boundary with a clip on either side always writes the level: the ordinary song path does
 * nothing while normalization is off and leaves a level alone that looks ducked, and a voice
 * trimmed well under the music looks exactly like that. Song to song stays on the ordinary path.
 */
internal class ClipVoiceSteering(
    private val currentIsClip: () -> Boolean,
    private val writeLevel: () -> Unit,
    private val songChange: () -> Unit,
) {
    private var wasClip = false

    fun onItemTransition() {
        val isClip = currentIsClip()
        val crossed = isClip || wasClip
        wasClip = isClip
        if (crossed) writeLevel() else songChange()
    }
}
