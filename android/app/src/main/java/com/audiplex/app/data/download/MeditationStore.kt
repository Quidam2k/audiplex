package com.audiplex.app.data.download

import android.content.ContentValues
import android.content.Context
import android.net.Uri
import android.os.Build
import android.provider.MediaStore
import android.util.Log

/**
 * Destination for meditation downloads: the SHARED audio collection, not
 * app-private storage. (#839)
 *
 * Audiobooks keep landing in getExternalFilesDir("downloads"), which is
 * app-scoped — and on Android 11+ that directory is unreadable by every
 * other app, via file path, via SAF, even with MANAGE_EXTERNAL_STORAGE.
 * Meditations have to be readable by the Pantheon companion app, so they
 * go through MediaStore into:
 *
 *     /storage/emulated/0/Music/Audiplex/Meditations/<Title>.m4a
 *
 * Writing there costs us no permission (an app may always insert its own
 * media). A reader needs only READ_MEDIA_AUDIO (API 33+) or
 * READ_EXTERNAL_STORAGE (API <= 32), and should locate these by querying
 * MediaStore.Audio.Media for RELATIVE_PATH LIKE 'Music/Audiplex/Meditations/%'
 * rather than by hardcoding the path — MediaStore is the index, and it
 * hands back TITLE, DURATION, SIZE and a readable content:// Uri per item.
 *
 * Trade-off accepted by Todd: these files are visible to other media apps.
 */
object MeditationStore {

    private const val TAG = "MeditationStore"

    /** Directory portion of the cross-app contract. Keep in sync with pantheon-android. */
    const val RELATIVE_PATH = "Music/Audiplex/Meditations"

    /**
     * Sources are .m4a today, so the extension is fixed rather than derived —
     * the library API does not expose a file extension and adding one was out
     * of scope for this stopgap. An .mp3 meditation would be stored with an
     * .m4a name; MIME-sniffing players still handle it, but the ingest side
     * should grow an extension field before that case is real.
     */
    private const val EXTENSION = "m4a"
    private const val MIME_TYPE = "audio/mp4"

    fun isContentUri(pathOrUri: String): Boolean = pathOrUri.startsWith("content://")

    /** Strip characters that are illegal in a display name, and bound the length. */
    fun sanitizeFileName(title: String): String {
        val cleaned = title
            .replace(Regex("""[\\/:*?"<>|]"""), "-")
            .replace(Regex("\\s+"), " ")
            .trim()
            .trimEnd('.')
            .take(120)
        return if (cleaned.isBlank()) "Meditation" else "$cleaned.$EXTENSION"
    }

    /**
     * Reserve a MediaStore entry and return its content:// Uri as a string,
     * or null if shared storage is unavailable (API < 29, where RELATIVE_PATH
     * does not exist — the caller falls back to the app-private path).
     *
     * The row is inserted IS_PENDING so other apps do not see a half-written
     * file; [publish] clears that once the bytes have landed.
     */
    fun createDestination(context: Context, title: String): String? {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return null

        val fileName = sanitizeFileName(title)
        deleteByDisplayName(context, fileName)

        val values = ContentValues().apply {
            put(MediaStore.Audio.Media.DISPLAY_NAME, fileName)
            put(MediaStore.Audio.Media.TITLE, title)
            put(MediaStore.Audio.Media.MIME_TYPE, MIME_TYPE)
            put(MediaStore.Audio.Media.RELATIVE_PATH, RELATIVE_PATH)
            put(MediaStore.Audio.Media.IS_PENDING, 1)
        }

        return try {
            val collection = MediaStore.Audio.Media
                .getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY)
            context.contentResolver.insert(collection, values)?.toString()
        } catch (e: Exception) {
            Log.w(TAG, "MediaStore insert failed for $fileName", e)
            null
        }
    }

    /** Clear IS_PENDING so the finished file becomes visible to other apps. */
    fun publish(context: Context, uriString: String) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return
        try {
            val values = ContentValues().apply {
                put(MediaStore.Audio.Media.IS_PENDING, 0)
            }
            context.contentResolver.update(Uri.parse(uriString), values, null, null)
        } catch (e: Exception) {
            Log.w(TAG, "Failed to publish $uriString", e)
        }
    }

    fun delete(context: Context, uriString: String) {
        try {
            context.contentResolver.delete(Uri.parse(uriString), null, null)
        } catch (e: Exception) {
            Log.w(TAG, "Failed to delete $uriString", e)
        }
    }

    /**
     * Remove any existing entry with this name before re-inserting, so
     * downloading the same meditation twice replaces it instead of leaving
     * MediaStore to disambiguate with "Title (1).m4a".
     */
    private fun deleteByDisplayName(context: Context, fileName: String) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return
        try {
            context.contentResolver.delete(
                MediaStore.Audio.Media.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY),
                "${MediaStore.Audio.Media.DISPLAY_NAME} = ? AND " +
                    "${MediaStore.Audio.Media.RELATIVE_PATH} LIKE ?",
                arrayOf(fileName, "$RELATIVE_PATH%")
            )
        } catch (e: Exception) {
            // Another app may own a same-named row; not ours to delete.
            Log.d(TAG, "No pre-existing entry removed for $fileName: ${e.message}")
        }
    }
}
