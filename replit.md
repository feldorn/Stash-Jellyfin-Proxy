# Overview

This is a full-stack web application built with a modern TypeScript stack, featuring a React frontend and Express backend. The application uses PostgreSQL with Drizzle ORM for data persistence and integrates with shadcn/ui components for the user interface. Additionally, the repository contains a Python proxy server (`stash_jellyfin_proxy.py`) that bridges Stash media server with Jellyfin-compatible clients like Infuse.

# User Preferences

Preferred communication style: Simple, everyday language.

# System Architecture

## Frontend Architecture

**Technology Stack**: React with Vite bundler and TypeScript

The frontend is built as a single-page application using React. The project uses Vite as the build tool and development server, configured to run on port 5000 during development. The application leverages shadcn/ui components (New York style variant) built on top of Radix UI primitives for accessible, customizable UI components.

**Styling**: TailwindCSS with CSS variables for theming, using a neutral base color scheme. The styling system supports component composition through class-variance-authority and clsx utilities.

**State Management**: TanStack Query (React Query) for server state management, handling data fetching, caching, and synchronization with the backend.

**Form Handling**: React Hook Form with Zod schema validation via @hookform/resolvers for type-safe form validation.

**Directory Structure**: 
- `client/src/` - Frontend source code
- `client/public/` - Static assets including OpenGraph images
- Component aliases configured via path mapping (@/, @/components, @/lib, etc.)

## Backend Architecture

**Technology Stack**: Node.js with Express and TypeScript

The backend is an Express server that serves both API endpoints and the built frontend in production. The server uses ESM (ES Modules) as indicated by `"type": "module"` in package.json.

**Development vs Production**:
- Development: Runs with tsx for TypeScript execution directly (`npm run dev`)
- Production: Builds to CommonJS bundle in dist/ directory (`npm run build` then `npm start`)

**Session Management**: PostgreSQL-backed sessions using connect-pg-simple for persistent session storage.

**API Layer**: RESTful API architecture with Express routes. The shared directory contains common schemas and types used across frontend and backend.

## Data Storage

**Primary Database**: PostgreSQL via Neon serverless driver (@neondatabase/serverless)

**ORM**: Drizzle ORM for type-safe database queries and migrations

- Schema definition: `shared/schema.ts`
- Migration files: `migrations/` directory
- Database dialect: PostgreSQL
- Schema push command: `npm run db:push`

**Schema Validation**: Drizzle-Zod integration for generating Zod schemas from Drizzle table definitions, ensuring consistency between database schema and runtime validation.

**Database Configuration**: Requires `DATABASE_URL` environment variable. The configuration enforces this requirement with an error if not provided.

## External Dependencies

**Third-Party Services**:

1. **Neon Database** - Serverless PostgreSQL hosting (primary database)
2. **Stash Media Server** - Adult content management system integrated via the Python proxy
   - URL: https://stash.feldorn.com
   - Authentication via API key
   - GraphQL API endpoint: /graphql-local

**Python Proxy Server** (stash_jellyfin_proxy.py):

- **Purpose**: Translates Stash's API to Jellyfin-compatible endpoints for clients like Infuse
- **Framework**: Starlette (ASGI) with Hypercorn server
- **Port**: 8096 (default Jellyfin port)
- **Authentication**: Simple shared API key authentication (user-1/infuse12345)
- **Configuration**: Loads from `/home/chris/.scripts.conf`
- **Key Features**:
  - Maps Stash scenes to Jellyfin movie items
  - Proxies video streams and images from Stash
  - Provides Jellyfin-compatible metadata responses
  - Supports GraphQL queries to Stash backend

**UI Component Libraries**:

1. **Radix UI** - Comprehensive collection of accessible, unstyled component primitives
2. **shadcn/ui** - Pre-styled Radix components with TailwindCSS
3. **Lucide Icons** - Icon library
4. **Embla Carousel** - Carousel/slider component
5. **cmdk** - Command palette component

**Development Tools**:

1. **Replit Development Plugins** (development only):
   - @replit/vite-plugin-runtime-error-modal - Enhanced error overlay
   - @replit/vite-plugin-cartographer - Code navigation
   - @replit/vite-plugin-dev-banner - Development banner

**Date/Time**: date-fns for date manipulation and formatting

**Build Process**: Custom build script (`script/build.ts`) that handles both frontend and backend compilation, producing a single distributable bundle in the dist/ directory.