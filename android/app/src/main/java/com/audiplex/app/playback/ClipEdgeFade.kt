package com.audiplex.app.playback

import androidx.annotation.OptIn
import androidx.media3.common.util.UnstableApi
import androidx.media3.exoplayer.audio.AudioSink
import androidx.media3.exoplayer.audio.ForwardingAudioSink
import kotlin.math.PI
import kotlin.math.sin

/**
 * #8197: DJ voice clips are queued as plain items (mediaId "djclip:...") and used to
 * hard-cut against the songs. The song before a clip now fades out over its last
 * OUT_MS and the song after one fades in over its first IN_MS (equal power); the clip
 * itself stays at full level. #5282: except its first CLIP_IN_MS, which cover the moment the
 * player is still at the song's volume, before the DJ voice level lands. Pure, for JVM tests.
 */
object ClipEdgeFade {
    const val OUT_MS = 2500L
    const val IN_MS = 1500L
    const val CLIP_IN_MS = 150L
    const val TICK_MS = 50L

    fun isClip(mediaId: String?): Boolean = mediaId?.startsWith("djclip:") == true

    fun level(
        prevIsClip: Boolean,
        curIsClip: Boolean,
        nextIsClip: Boolean,
        positionMs: Long,
        durationMs: Long
    ): Float {
        if (curIsClip) {
            val p = positionMs.coerceAtLeast(0L)
            return if (p < CLIP_IN_MS) sin(PI / 2 * p.toDouble() / CLIP_IN_MS).toFloat() else 1f
        }
        var g = 1f
        if (nextIsClip && durationMs > 0) {
            val rem = durationMs - positionMs
            if (rem < OUT_MS) {
                val t = (rem.toDouble() / OUT_MS).coerceIn(0.0, 1.0)
                g = g.coerceAtMost(sin(PI / 2 * t).toFloat())
            }
        }
        if (prevIsClip) {
            val p = positionMs.coerceAtLeast(0L)
            if (p < IN_MS) {
                g = g.coerceAtMost(sin(PI / 2 * p.toDouble() / IN_MS).toFloat())
            }
        }
        return g.coerceIn(0f, 1f)
    }
}

/**
 * #8197: multiplies the player's volume by the clip-edge level at the AudioTrack, so it
 * lands at output time (no buffer lead) and never touches player.volume, which
 * AudioFocusManager's #3597 duck ramps, the dial and the sleep fade own.
 * Not thread-safe, like the sink it wraps: call [setEdge] on the playback thread.
 */
@OptIn(UnstableApi::class)
class ClipEdgeSink(sink: AudioSink) : ForwardingAudioSink(sink) {
    private var volume = 1f
    private var edge = 1f

    override fun setVolume(volume: Float) {
        this.volume = volume
        super.setVolume(volume * edge)
    }

    fun setEdge(edge: Float) {
        if (edge == this.edge) return
        this.edge = edge
        super.setVolume(volume * edge)
    }
}
