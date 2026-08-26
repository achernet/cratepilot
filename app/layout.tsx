import type { Metadata } from 'next';
import { Space_Grotesk, IBM_Plex_Mono } from 'next/font/google';
import './globals.css';

const sans = Space_Grotesk({ variable: '--font-sans', subsets: ['latin'] });
const mono = IBM_Plex_Mono({ variable: '--font-mono', subsets: ['latin'], weight: ['400', '500', '600'] });

export const metadata: Metadata = {
  metadataBase: new URL('https://cratepilot.chernetz.com'),
  title: {
    default: 'CratePilot — Build a set you can trust',
    template: '%s · CratePilot',
  },
  description: 'An explainable DJ set planner built from audio analysis and patterns learned from real transitions.',
  openGraph: {
    title: 'CratePilot — Build a set you can trust',
    description: 'Compose, understand, audition, and prepare a first-booth DJ set.',
    url: 'https://cratepilot.chernetz.com',
    siteName: 'CratePilot',
    images: [{ url: '/og.png', width: 1731, height: 909, alt: 'CratePilot — Build a set you can trust' }],
    type: 'website',
  },
  twitter: {
    card: 'summary_large_image',
    title: 'CratePilot — Build a set you can trust',
    description: 'An explainable DJ-set planner and first-booth workflow.',
    images: ['/og.png'],
  },
  robots: { index: true, follow: true },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body className={`${sans.variable} ${mono.variable}`}>{children}</body></html>;
}
